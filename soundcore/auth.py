"""Single-admin session login. The password hash is created from configuration, never from a constant."""

from __future__ import annotations

from functools import wraps

from flask import abort, g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from soundcore.models import User, utcnow


def ensure_admin(db, email: str, password: str, *, reset: bool = False) -> User | None:
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    user = db.query(User).filter_by(email=email).one_or_none()
    if user is None:
        user = User(email=email, password_hash=generate_password_hash(password), role="admin")
        db.add(user)
        db.flush()
        return user
    if reset:
        user.password_hash = generate_password_hash(password)
        db.flush()
    return user


def authenticate(db, email: str, password: str) -> User | None:
    email = (email or "").strip().lower()
    if not email or not password:
        return None
    user = db.query(User).filter_by(email=email).one_or_none()
    if user is None or not check_password_hash(user.password_hash, password):
        return None
    user.last_login_at = utcnow()
    db.flush()
    return user


def login_user(user: User) -> None:
    session.clear()
    session["user_id"] = user.id
    session["user_email"] = user.email
    session.permanent = True


def logout_user() -> None:
    session.clear()


def current_user():
    return getattr(g, "current_user", None)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            if request.path.startswith("/api") or request.accept_mimetypes.best == "application/json":
                return jsonify({"error": "Sign in required"}), 401
            return redirect(url_for("pages.login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


def require_csrf() -> None:
    if request.method != "POST":
        return
    token = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not token or token != session.get("csrf"):
        abort(400, description="CSRF token missing")
