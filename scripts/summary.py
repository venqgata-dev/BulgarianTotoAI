"""One-off data summary (invoked during milestone verification)."""
from sqlalchemy import func, select

from main import bootstrap
from app.database.models import Draw, Game

config, database = bootstrap()
with database.session() as session:
    rows = session.execute(
        select(
            Game.code,
            func.count(Draw.id),
            func.min(Draw.draw_date),
            func.max(Draw.draw_date),
            func.count(func.distinct(Draw.source)),
        )
        .join(Draw, Draw.game_id == Game.id)
        .group_by(Game.code)
    ).all()
    for code, count, first, last, _ in rows:
        by_source = session.execute(
            select(Draw.source, func.count(Draw.id))
            .join(Game, Game.id == Draw.game_id)
            .where(Game.code == code)
            .group_by(Draw.source)
        ).all()
        sources = ", ".join(f"{s}={c}" for s, c in by_source)
        print(f"{code}: {count} draws, {first} .. {last}  ({sources})")
database.dispose()
