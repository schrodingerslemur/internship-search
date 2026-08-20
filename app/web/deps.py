"""Request dependencies."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.services import auth


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """The signed-in account, as a live ORM object.

    The middleware has already verified the cookie's signature, so this only
    re-reads the row -- which also catches an account deleted or deactivated
    since the cookie was issued.
    """
    user_id = getattr(request.state, "user_id", None)
    user = auth.get_user(db, user_id) if user_id else None
    if user is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return user
