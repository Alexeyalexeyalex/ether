"""Isolated local preview. Never opens the user's instance/efir.db."""
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["EFIR_INSTANCE_DIR"] = tempfile.mkdtemp(prefix="efir-test-")

from app import create_app, socketio
from app.database import bootstrap
from app.models import User, Room, Membership, db

app = create_app()
with app.app_context():
    bootstrap()
    room = Room.query.first()
    for username, name in [("alice", "Алиса"), ("boris", "Борис"), ("vera", "Вера")]:
        user = User(username=username, display_name=name)
        user.set_password("local-test-only")
        db.session.add(user)
        db.session.flush()
        db.session.add(Membership(user_id=user.id, room_id=room.id))
    db.session.commit()

if __name__ == "__main__":
    socketio.run(app, host="127.0.0.1", port=2080, allow_unsafe_werkzeug=True, use_reloader=False)
