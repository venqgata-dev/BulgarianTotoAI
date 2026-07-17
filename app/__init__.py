"""Bulgarian Toto AI - desktop application for historical Bulgarian Toto analysis.

Package layout:
    app.config    - configuration system
    app.database  - SQLAlchemy ORM models, engine, repositories
    app.models    - framework-free domain objects
    app.scraper   - toto.bg historical results scraper
    app.services  - logging, validation and other application services
    app.ui        - PySide6 application shell
    app.analysis  - reserved for statistical / ML models (future milestones)
"""

__version__ = "0.1.0"
APP_NAME = "Bulgarian Toto AI"
