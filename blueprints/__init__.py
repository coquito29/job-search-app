"""Flask blueprints — one module per functional area.

Routes that used to live in app.py are migrating here over time. Pattern:

    from blueprints.applications import bp as applications_bp
    app.register_blueprint(applications_bp)

Each blueprint imports its dependencies from db.py / scoring.py / etc. —
no circular imports back to app.py.
"""
