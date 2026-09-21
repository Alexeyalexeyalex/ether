/* ЭФИР — шумодав.
 *
 * Спектральное вычитание: пока человек молчит, копим профиль постоянного
 * фона (вентилятор, гул, шипение), потом вычитаем его из каждого кадра.
 * Речь при этом почти не страдает — у неё энергия сосредоточена в других
 * полосах и меняется во времени, а фон ровный.
 *
 * Работает через AudioWorklet: обработка идёт в отдельном аудиопотоке и не
 * дёргается от загрузки интерфейса. Если браузер его не поддерживает,
 * вызывающая сторона просто не подключает узел — связь важнее фильтра.
 */

class Denoiser extends AudioWorkletProcessor {
  static get parameterDescriptors() {
    return [{
      name: 'strength',
      defaultValue: 1,
      minValue: 0,
      maxValue: 2,
      automationRate: 'k-rate'
    }];
  }

  constructor() {
    super();
    this.SIZE = 512;
    this.HOP = 256;
    this.BINS = this.SIZE / 2 + 1;

    /* Окно применяется дважды — на анализе и на синтезе, — поэтому берём
       корень из Ханна: их произведение даёт ровно Ханн, а он при шаге в
       половину кадра суммируется в единицу. С полным Ханном на обоих концах
       сумма перекрытий гуляла бы от 0.5 до 1.0, то есть сигнал получал бы
       амплитудную модуляцию частотой fs/HOP — она и звучит как металлический,
       «роботизированный» призвук. */
    this.window = new Float32Array(this.SIZE);
    for (let i = 0; i < this.SIZE; i++) {
      const hann = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / this.SIZE);
      this.window[i] = Math.sqrt(hann);
    }

    this.input = new Float32Array(this.SIZE);
    this.output = new Float32Array(this.SIZE);
    this.filled = 0;

    // кольцевой буфер: shift() на обычном массиве в аудиопотоке слишком дорог
    this.ring = new Float32Array(this.SIZE * 4);
    this.readAt = 0;
    this.writeAt = 0;
    this.ready = 0;

    this.re = new Float32Array(this.SIZE);
    this.im = new Float32Array(this.SIZE);
    this.profile = new Float32Array(this.BINS);
    this.gains = new Float32Array(this.BINS).fill(1);
    this.smoothed = new Float32Array(this.BINS).fill(1);
    this.calm = 0;
    this.learned = false;

    this.buildTables();
  }

  /* Таблицы поворотов и битреверса считаем один раз: без них
     преобразование пришлось бы делать в разы медленнее. */
  buildTables() {
    const n = this.SIZE;
    this.cos = new Float32Array(n / 2);
    this.sin = new Float32Array(n / 2);
    for (let i = 0; i < n / 2; i++) {
      this.cos[i] = Math.cos((-2 * Math.PI * i) / n);
      this.sin[i] = Math.sin((-2 * Math.PI * i) / n);
    }
    this.rev = new Uint16Array(n);
    const bits = Math.round(Math.log2(n));
    for (let i = 0; i < n; i++) {
      let x = i, r = 0;
      for (let b = 0; b < bits; b++) { r = (r << 1) | (x & 1); x >>= 1; }
      this.rev[i] = r;
    }
  }

  fft(re, im, inverse) {
    const n = this.SIZE;
    for (let i = 0; i < n; i++) {
      const j = this.rev[i];
      if (j > i) {
        let t = re[i]; re[i] = re[j]; re[j] = t;
        t = im[i]; im[i] = im[j]; im[j] = t;
      }
    }
    for (let size = 2; size <= n; size <<= 1) {
      const half = size >> 1;
      const step = n / size;
      for (let i = 0; i < n; i += size) {
        for (let j = i, k = 0; j < i + half; j++, k += step) {
          const c = this.cos[k];
          const s = inverse ? -this.sin[k] : this.sin[k];
          const tr = re[j + half] * c - im[j + half] * s;
          const ti = re[j + half] * s + im[j + half] * c;
          re[j + half] = re[j] - tr;
          im[j + half] = im[j] - ti;
          re[j] += tr;
          im[j] += ti;
        }
      }
    }
    if (inverse) {
      for (let i = 0; i < n; i++) { re[i] /= n; im[i] /= n; }
    }
  }

  frame(strength) {
    const n = this.SIZE;
    for (let i = 0; i < n; i++) {
      this.re[i] = this.input[i] * this.window[i];
      this.im[i] = 0;
    }
    this.fft(this.re, this.im, false);

    // громкость кадра — по ней понимаем, тишина сейчас или речь
    let energy = 0;
    for (let k = 0; k < this.BINS; k++) {
      energy += Math.hypot(this.re[k], this.im[k]);
    }
    energy /= this.BINS;

    const quiet = !this.learned || energy < this.calm * 1.6;
    if (quiet) {
      // профиль обновляем только в тишине, иначе он вберёт в себя голос
      const rate = this.learned ? 0.05 : 0.35;
      for (let k = 0; k < this.BINS; k++) {
        const mag = Math.hypot(this.re[k], this.im[k]);
        this.profile[k] += (mag - this.profile[k]) * rate;
      }
      this.calm = this.learned ? this.calm + (energy - this.calm) * 0.05 : energy;
      this.learned = true;
    }

    if (strength > 0.001) {
      for (let k = 0; k < this.BINS; k++) {
        const mag = Math.hypot(this.re[k], this.im[k]) || 1e-9;
        const clean = mag - strength * this.profile[k];
        // остаток не давим в ноль: полная тишина в полосе даёт «бульканье»
        const floor = mag * Math.max(0.2, 1 - strength);
        this.gains[k] = Math.max(clean, floor, 0) / mag;
      }

      /* Сглаживаем усиление по соседним полосам. Без этого соседние частоты
         получают сильно разное усиление, и остатки шума собираются в короткие
         тоны — тот самый музыкальный шум. */
      let previous = this.gains[0];
      for (let k = 1; k < this.BINS - 1; k++) {
        const current = this.gains[k];
        const mixed = 0.25 * previous + 0.5 * current + 0.25 * this.gains[k + 1];
        previous = current;
        this.gains[k] = mixed;
      }

      for (let k = 0; k < this.BINS; k++) {
        // и сглаживаем во времени, чтобы усиление не дёргалось между кадрами
        this.smoothed[k] += (this.gains[k] - this.smoothed[k]) * 0.4;
        const gain = this.smoothed[k];
        this.re[k] *= gain;
        this.im[k] *= gain;
        const mirror = this.SIZE - k;
        if (k > 0 && mirror < this.SIZE) {
          this.re[mirror] *= gain;
          this.im[mirror] *= gain;
        }
      }
    }

    this.fft(this.re, this.im, true);
    for (let i = 0; i < n; i++) {
      this.output[i] += this.re[i] * this.window[i];
    }
  }

  process(inputs, outputs, parameters) {
    const source = inputs[0] && inputs[0][0];
    const sink = outputs[0] && outputs[0][0];
    if (!sink) return true;
    if (!source) { sink.fill(0); return true; }

    const strength = parameters.strength[0];

    for (let i = 0; i < source.length; i++) {
      this.input[this.filled++] = source[i];
      if (this.filled === this.SIZE) {
        this.frame(strength);
        // отдаём первую половину, вторая ждёт следующего кадра
        for (let j = 0; j < this.HOP; j++) {
          this.ring[this.writeAt] = this.output[j];
          this.writeAt = (this.writeAt + 1) % this.ring.length;
          this.ready++;
        }
        this.output.copyWithin(0, this.HOP);
        this.output.fill(0, this.HOP);
        this.input.copyWithin(0, this.HOP);
        this.filled = this.HOP;
      }
    }

    for (let i = 0; i < sink.length; i++) {
      if (this.ready > 0) {
        sink[i] = this.ring[this.readAt];
        this.readAt = (this.readAt + 1) % this.ring.length;
        this.ready--;
      } else {
        sink[i] = 0;   // первые кадры, пока копится задержка окна
      }
    }
    return true;
  }
}

registerProcessor('denoiser', Denoiser);
