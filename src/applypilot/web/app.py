"""Flask application factory for ApplyPilot web UI."""

from pathlib import Path

from flask import Flask, render_template

from applypilot.database import get_connection, get_stats


def create_app() -> Flask:
    """Create and configure the Flask application."""
    template_dir = Path(__file__).parent / "templates"
    static_dir = Path(__file__).parent / "static"

    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir),
    )
    app.config["SECRET_KEY"] = "applypilot-local-dev"

    # Register API blueprint
    from applypilot.web.api import api
    app.register_blueprint(api)

    # Page routes
    @app.route("/")
    def index():
        """Main job browser page."""
        conn = get_connection()
        stats = get_stats(conn)

        # Get filter options
        sites = conn.execute(
            "SELECT DISTINCT site FROM jobs WHERE site IS NOT NULL ORDER BY site"
        ).fetchall()
        countries = conn.execute(
            "SELECT DISTINCT country_code FROM jobs "
            "WHERE country_code IS NOT NULL ORDER BY country_code"
        ).fetchall()
        company_tags = conn.execute(
            "SELECT DISTINCT company_tag FROM jobs "
            "WHERE company_tag IS NOT NULL ORDER BY company_tag"
        ).fetchall()

        return render_template(
            "index.html",
            stats=stats,
            sites=[r[0] for r in sites],
            countries=[r[0] for r in countries],
            company_tags=[r[0] for r in company_tags],
        )

    @app.route("/tracker")
    def tracker():
        """Kanban tracker page."""
        return render_template("tracker.html")

    return app
