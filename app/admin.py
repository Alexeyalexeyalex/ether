"""Админ-панель: правка любой таблицы базы + SQL-консоль."""

from datetime import datetime
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import Boolean, DateTime, Integer, Text, func, inspect, text

from .models import Channel, Membership, Message, Room, User, VoiceBan, db

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

TABLES = {
    "users": {"model": User, "label": "Пользователи", "order": User.id},
    "rooms": {"model": Room, "label": "Комнаты", "order": Room.id},
    "channels": {"model": Channel, "label": "Каналы", "order": Channel.id},
    "memberships": {"model": Membership, "label": "Участники комнат", "order": Membership.id},
    "messages": {"model": Message, "label": "Сообщения", "order": Message.id.desc()},
    "voice_bans": {"model": VoiceBan, "label": "Баны в голосовых", "order": VoiceBan.id.desc()},
}

PER_PAGE = 40
HIDDEN_IN_LIST = {"password_hash"}


@admin_bp.context_processor
def inject_tables():
    return {"tables": TABLES}


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def get_table(name: str) -> dict:
    if name not in TABLES:
        abort(404)
    return TABLES[name]


def columns_of(model):
    return list(model.__table__.columns)


def coerce(column, raw: str):
    """Приводит строку из формы к типу колонки."""
    if raw is None:
        raw = ""
    raw = raw.strip()

    if isinstance(column.type, Boolean):
        return raw in ("1", "on", "true", "True", "да")
    if raw == "":
        return None if column.nullable else ("" if isinstance(column.type, Text) else None)
    if isinstance(column.type, Integer):
        try:
            return int(raw)
        except ValueError:
            raise ValueError(f"«{column.name}»: нужно целое число")
    if isinstance(column.type, DateTime):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        raise ValueError(f"«{column.name}»: дата в формате ГГГГ-ММ-ДД ЧЧ:ММ")
    return raw


def field_value(row, column):
    value = getattr(row, column.name, None)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if value is None:
        return ""
    return value


# --------------------------------------------------------------------------- страницы


@admin_bp.get("/")
@admin_required
def dashboard():
    stats = []
    for key, meta in TABLES.items():
        stats.append(
            {
                "key": key,
                "label": meta["label"],
                "count": db.session.query(func.count(meta["model"].id)).scalar() or 0,
            }
        )
    recent = Message.query.order_by(Message.id.desc()).limit(8).all()
    newcomers = User.query.order_by(User.id.desc()).limit(8).all()
    return render_template("admin/dashboard.html", stats=stats, recent=recent, newcomers=newcomers)


@admin_bp.get("/t/<table>")
@admin_required
def table_list(table: str):
    meta = get_table(table)
    model = meta["model"]
    page = max(request.args.get("page", 1, type=int), 1)
    search = (request.args.get("q") or "").strip()

    query = model.query
    if search:
        filters = [
            col.ilike(f"%{search}%")
            for col in columns_of(model)
            if isinstance(col.type, Text) or col.type.__class__.__name__ == "String"
        ]
        if search.isdigit():
            filters.append(model.id == int(search))
        if filters:
            query = query.filter(db.or_(*filters))

    total = query.count()
    rows = (
        query.order_by(meta["order"])
        .limit(PER_PAGE)
        .offset((page - 1) * PER_PAGE)
        .all()
    )

    cols = [c for c in columns_of(model) if c.name not in HIDDEN_IN_LIST]
    return render_template(
        "admin/table.html",
        table=table,
        meta=meta,
        rows=rows,
        cols=cols,
        page=page,
        pages=max((total + PER_PAGE - 1) // PER_PAGE, 1),
        total=total,
        search=search,
        field_value=field_value,
    )


@admin_bp.route("/t/<table>/new", methods=["GET", "POST"])
@admin_required
def row_create(table: str):
    meta = get_table(table)
    model = meta["model"]
    cols = [c for c in columns_of(model) if c.name != "id"]

    if request.method == "POST":
        row = model()
        try:
            for col in cols:
                if col.name == "password_hash":
                    continue
                if col.name in request.form or isinstance(col.type, Boolean):
                    value = coerce(col, request.form.get(col.name))
                    if value is not None or col.nullable:
                        setattr(row, col.name, value)
            new_password = (request.form.get("__password") or "").strip()
            if hasattr(row, "set_password") and new_password:
                row.set_password(new_password)
            db.session.add(row)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return render_template(
                "admin/form.html", table=table, meta=meta, cols=cols, row=None,
                form=request.form, field_value=field_value,
            ), 400
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            flash(f"Не удалось сохранить: {exc}", "error")
            return render_template(
                "admin/form.html", table=table, meta=meta, cols=cols, row=None,
                form=request.form, field_value=field_value,
            ), 400

        flash("Запись создана.", "ok")
        return redirect(url_for("admin.table_list", table=table))

    return render_template(
        "admin/form.html", table=table, meta=meta, cols=cols, row=None,
        form={}, field_value=field_value,
    )


@admin_bp.route("/t/<table>/<int:row_id>", methods=["GET", "POST"])
@admin_required
def row_edit(table: str, row_id: int):
    meta = get_table(table)
    model = meta["model"]
    row = db.session.get(model, row_id)
    if row is None:
        abort(404)
    cols = [c for c in columns_of(model) if c.name != "id"]

    if request.method == "POST":
        try:
            for col in cols:
                if col.name == "password_hash":
                    continue
                if isinstance(col.type, Boolean):
                    setattr(row, col.name, col.name in request.form)
                elif col.name in request.form:
                    setattr(row, col.name, coerce(col, request.form.get(col.name)))
            new_password = (request.form.get("__password") or "").strip()
            if hasattr(row, "set_password") and new_password:
                row.set_password(new_password)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return render_template(
                "admin/form.html", table=table, meta=meta, cols=cols, row=row,
                form=request.form, field_value=field_value,
            ), 400
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            flash(f"Не удалось сохранить: {exc}", "error")
            return render_template(
                "admin/form.html", table=table, meta=meta, cols=cols, row=row,
                form=request.form, field_value=field_value,
            ), 400

        flash("Изменения сохранены.", "ok")
        return redirect(url_for("admin.table_list", table=table))

    return render_template(
        "admin/form.html", table=table, meta=meta, cols=cols, row=row,
        form={}, field_value=field_value,
    )


@admin_bp.post("/t/<table>/<int:row_id>/delete")
@admin_required
def row_delete(table: str, row_id: int):
    meta = get_table(table)
    row = db.session.get(meta["model"], row_id)
    if row is None:
        flash("Запись уже удалена.", "warning")
        return redirect(url_for("admin.table_list", table=table))

    if isinstance(row, User):
        if row.id == current_user.id:
            flash("Нельзя удалить собственный аккаунт.", "error")
            return redirect(url_for("admin.table_list", table=table))
        if row.is_admin and User.query.filter_by(is_admin=True).count() <= 1:
            flash("Это последний администратор — удаление отменено.", "error")
            return redirect(url_for("admin.table_list", table=table))

    db.session.delete(row)
    db.session.commit()
    flash("Запись удалена.", "ok")
    return redirect(url_for("admin.table_list", table=table, page=request.form.get("page", 1)))


@admin_bp.route("/sql", methods=["GET", "POST"])
@admin_required
def sql_console():
    query = request.form.get("sql", "") if request.method == "POST" else ""
    columns: list[str] = []
    rows: list[tuple] = []
    info = ""

    if request.method == "POST" and query.strip():
        try:
            result = db.session.execute(text(query))
            if result.returns_rows:
                columns = list(result.keys())
                rows = [tuple(r) for r in result.fetchmany(300)]
                info = f"Строк получено: {len(rows)}"
            else:
                db.session.commit()
                info = f"Запрос выполнен. Затронуто строк: {result.rowcount}"
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            flash(f"Ошибка SQL: {exc}", "error")

    schema = {}
    inspector = inspect(db.engine)
    for table_name in inspector.get_table_names():
        schema[table_name] = [c["name"] for c in inspector.get_columns(table_name)]

    return render_template(
        "admin/sql.html", sql=query, columns=columns, rows=rows, info=info, schema=schema
    )
