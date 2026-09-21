"""Самоподписанный сертификат для HTTPS.

Werkzeug умеет делать сертификат на лету (`ssl_context="adhoc"`), но каждый
запуск сервера выпускает новый — и браузер каждый раз заново требует принять
исключение. Поэтому выпускаем свой и храним в instance/: он переживает
перезапуски, а при желании его можно один раз добавить в доверенные,
и предупреждение исчезнет совсем.
"""

import datetime
import ipaddress
import os

from .paths import INSTANCE_DIR, ensure_instance_dir

CERT_FILE = os.path.join(INSTANCE_DIR, "cert.pem")
KEY_FILE = os.path.join(INSTANCE_DIR, "key.pem")

VALID_DAYS = 397  # дольше браузеры сертификатам не верят


class CryptographyMissing(RuntimeError):
    pass


def _load_crypto():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        return x509, hashes, serialization, rsa, NameOID
    except ImportError as exc:  # pragma: no cover
        raise CryptographyMissing(
            "Для HTTPS нужен пакет cryptography: pip install cryptography"
        ) from exc


def _as_san(x509, host: str):
    """Имя в сертификате: IP-адреса и доменные имена оформляются по-разному."""
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def _covered_hosts(cert) -> set[str]:
    from cryptography import x509 as _x509

    try:
        san = cert.extensions.get_extension_for_class(_x509.SubjectAlternativeName).value
    except Exception:  # noqa: BLE001
        return set()
    names = {str(n) for n in san.get_values_for_type(_x509.DNSName)}
    names |= {str(ip) for ip in san.get_values_for_type(_x509.IPAddress)}
    return names


def _still_valid(cert, hosts: set[str]) -> bool:
    from cryptography import x509 as _x509  # noqa: F401

    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        not_after = cert.not_valid_after_utc
    except AttributeError:  # cryptography < 42
        not_after = cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    if not_after < now + datetime.timedelta(days=2):
        return False
    return hosts.issubset(_covered_hosts(cert))


def ensure_certificate(hosts) -> tuple[str, str]:
    """Возвращает пути (сертификат, ключ), выпуская их при необходимости."""
    x509, hashes, serialization, rsa, NameOID = _load_crypto()
    ensure_instance_dir()

    wanted = {h for h in hosts if h}
    wanted |= {"localhost", "127.0.0.1", "::1"}

    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        try:
            with open(CERT_FILE, "rb") as fh:
                existing = x509.load_pem_x509_certificate(fh.read())
            if _still_valid(existing, wanted):
                return CERT_FILE, KEY_FILE
        except Exception:  # noqa: BLE001
            pass  # битый или устаревший — выпустим заново

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Efir Local"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Efir"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=VALID_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([_as_san(x509, h) for h in sorted(wanted)]),
            critical=False,
        )
        # CA=True, чтобы сертификат можно было положить в доверенные корневые
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, key_encipherment=True, key_cert_sign=True,
                content_commitment=False, data_encipherment=False, key_agreement=False,
                crl_sign=False, encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    with open(KEY_FILE, "wb") as fh:
        fh.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    with open(CERT_FILE, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))

    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass  # на Windows права выставляются иначе

    return CERT_FILE, KEY_FILE
