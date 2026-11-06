"""Sales Data Analysis System - Flask API + static frontend.  Run: python app.py  ->  http://127.0.0.1:5000"""
import csv, io, json, os, re, sqlite3, uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, request, send_from_directory
import services as sv

load_dotenv()
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (product_id {pk}, product_name VARCHAR(150) NOT NULL UNIQUE, category VARCHAR(100) NOT NULL);
CREATE TABLE IF NOT EXISTS import_logs (import_id {pk}, filename VARCHAR(255) NOT NULL, imported_at DATETIME NOT NULL,
  total_rows INT NOT NULL, inserted_rows INT NOT NULL, duplicate_rows INT NOT NULL, error_rows INT NOT NULL, error_report {longtext});
CREATE TABLE IF NOT EXISTS sales (sale_id {pk}, product_id INT NOT NULL, sale_date DATE NOT NULL, quantity INT NOT NULL CHECK (quantity > 0),
  unit_price DECIMAL(12,2) NOT NULL CHECK (unit_price >= 0), region VARCHAR(100) NOT NULL, row_hash CHAR(40) NOT NULL UNIQUE, import_id INT NULL,
  FOREIGN KEY (product_id) REFERENCES products(product_id), FOREIGN KEY (import_id) REFERENCES import_logs(import_id) ON DELETE SET NULL)
"""

def default_config():
    return dict(DB_ENGINE=os.getenv("DB_ENGINE", "auto"), DB_HOST=os.getenv("DB_HOST", "localhost"),
                DB_PORT=int(os.getenv("DB_PORT", 3306)), DB_USER=os.getenv("DB_USER", "root"),
                DB_PASSWORD=os.getenv("DB_PASSWORD", ""), DB_NAME=os.getenv("DB_NAME", "sales_analysis"),
                SQLITE_PATH=os.getenv("SQLITE_PATH", "sales.db"), MAX_CONTENT_LENGTH=5 * 1024 * 1024)

def _clean(v):
    if isinstance(v, Decimal): return float(v)
    if isinstance(v, (datetime, date)): return v.isoformat(sep=" ") if isinstance(v, datetime) else v.isoformat()
    return v

class DB:
    """Tiny wrapper so the same '?' SQL runs on MySQL (PyMySQL) and SQLite."""
    def __init__(self, cfg, database=True):
        self.my = cfg["DB_ENGINE"] == "mysql"
        if self.my:
            import pymysql, pymysql.cursors
            self.conn = pymysql.connect(host=cfg["DB_HOST"], port=cfg["DB_PORT"], user=cfg["DB_USER"], password=cfg["DB_PASSWORD"],
                                        database=cfg["DB_NAME"] if database else None, charset="utf8mb4",
                                        cursorclass=pymysql.cursors.DictCursor)
        else:
            self.conn = sqlite3.connect(cfg["SQLITE_PATH"]); self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
    def _run(self, sql, params=(), many=False):
        cur = self.conn.cursor()
        (cur.executemany if many else cur.execute)(sql.replace("?", "%s") if self.my else sql, params)
        return cur
    def query(self, sql, params=()):
        cur = self._run(sql, params); rows = [{k: _clean(v) for k, v in dict(r).items()} for r in cur.fetchall()]; cur.close(); return rows
    def exec(self, sql, params=(), many=False):
        cur = self._run(sql, params, many); last, n = cur.lastrowid, cur.rowcount; cur.close(); return last if not many else n

def init_schema(cfg):
    if cfg["DB_ENGINE"] == "mysql":
        boot = DB(cfg, database=False)
        boot.exec(f"CREATE DATABASE IF NOT EXISTS `{cfg['DB_NAME']}` CHARACTER SET utf8mb4"); boot.conn.commit(); boot.conn.close()
        pk, lt = "INT AUTO_INCREMENT PRIMARY KEY", "LONGTEXT"
    else: pk, lt = "INTEGER PRIMARY KEY AUTOINCREMENT", "TEXT"
    db = DB(cfg)
    for stmt in SCHEMA.format(pk=pk, longtext=lt).split(";\n"):
        if stmt.strip(): db.exec(stmt)
    db.conn.commit(); db.conn.close()

class ApiError(Exception):
    def __init__(self, msg, status=400, **extra): self.msg, self.status, self.extra = msg, status, extra

def create_app(overrides=None):
    cfg = default_config(); cfg.update(overrides or {})
    engine = cfg["DB_ENGINE"]
    if engine in ("auto", "mysql"):
        try:
            init_schema({**cfg, "DB_ENGINE": "mysql"}); cfg["DB_ENGINE"] = "mysql"
            print(f"* Using MySQL database '{cfg['DB_NAME']}' on {cfg['DB_HOST']}:{cfg['DB_PORT']}")
        except Exception as e:
            hint = ("The MySQL username/password in backend/.env is wrong (or the user is not allowed to log in)." if "1045" in str(e)
                    else "MySQL is not running / not reachable (check DB_HOST and DB_PORT), or PyMySQL is not installed.")
            msg = f"MySQL connection failed: {e}\n  -> {hint}"
            if engine == "mysql": raise SystemExit(msg + "\n  Fix backend/.env, or set DB_ENGINE=auto to fall back to SQLite.")
            print("\n" + msg + "\n  -> Falling back to a local SQLite file (backend/sales.db) so the app still works.\n")
            cfg["DB_ENGINE"] = "sqlite"; init_schema(cfg)
    else:
        init_schema(cfg)
    app = Flask(__name__, static_folder=None); app.config.update(cfg)

    def db():
        if "db" not in g: g.db = DB(cfg)
        return g.db

    @app.teardown_appcontext
    def _close(_):
        d = g.pop("db", None)
        if d: d.conn.close()

    @app.errorhandler(ApiError)
    def _api_err(e): return jsonify(error=e.msg, **e.extra), e.status
    @app.errorhandler(413)
    def _big(_): return jsonify(error="File too large (limit 5 MB)."), 413
    @app.errorhandler(404)
    def _404(_):
        return (jsonify(error="Not found"), 404) if request.path.startswith("/api") else ("Not found", 404)
    @app.errorhandler(500)
    def _500(e): return jsonify(error="Server error: " + str(getattr(e, "original_exception", e))), 500

    def filters():
        f = {k: request.args.get(k, "").strip() for k in ("start", "end", "category", "region")}
        for k in ("start", "end"):
            if f[k] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f[k]): raise ApiError(f"'{k}' must be YYYY-MM-DD")
        return f

    @app.get("/api/health")
    def health(): return jsonify(status="ok", engine=cfg["DB_ENGINE"])

    # ---- import ----
    @app.get("/api/template.csv")
    def template():
        body = ("product_name,category,sale_date,quantity,unit_price,region\nLaptop Pro,Electronics,2026-01-15,2,55000.00,North\n"
                "Cotton T-Shirt,Apparel,2026-01-16,10,499.00,South\n")
        return Response(body, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=sales_template.csv"})

    @app.post("/api/import")
    def import_file():
        f = request.files.get("file")
        if not f or not f.filename: raise ApiError("No file uploaded (form field 'file').")
        if not f.filename.lower().endswith(".csv"): raise ApiError("Only .csv files are accepted.")
        res = sv.import_csv(db(), f.filename, f.read())
        if res.get("fatal"): raise ApiError(res["fatal"], required_columns=sv.REQUIRED)
        return jsonify(res), 201

    @app.get("/api/imports")
    def imports():
        return jsonify(db().query("SELECT import_id, filename, imported_at, total_rows, inserted_rows, duplicate_rows, error_rows FROM import_logs ORDER BY import_id DESC LIMIT 50"))

    @app.get("/api/imports/<int:iid>/report.csv")
    def report(iid):
        r = db().query("SELECT error_report FROM import_logs WHERE import_id=?", (iid,))
        if not r: raise ApiError("Import not found", 404)
        out = io.StringIO(); w = csv.writer(out); w.writerow(["line", "type", "reason", "original_row"])
        for e in json.loads(r[0]["error_report"] or "[]"): w.writerow([e["line"], e["type"], e["reason"], e["row"]])
        return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=import_{iid}_report.csv"})

    # ---- products ----
    @app.get("/api/products")
    def products(): return jsonify(db().query("SELECT product_id, product_name, category FROM products ORDER BY product_name"))

    @app.post("/api/products")
    def add_product():
        d = request.get_json(silent=True) or {}
        name, cat = " ".join(str(d.get("product_name", "")).split()), str(d.get("category", "")).strip().title()
        if not name or not cat: raise ApiError("product_name and category are required.")
        if db().query("SELECT 1 AS x FROM products WHERE LOWER(product_name)=?", (name.lower(),)): raise ApiError("Product already exists.", 409)
        pid = db().exec("INSERT INTO products (product_name, category) VALUES (?,?)", (name, cat)); db().conn.commit()
        return jsonify(product_id=pid, product_name=name, category=cat), 201

    @app.delete("/api/products/<int:pid>")
    def del_product(pid):
        if db().query("SELECT 1 AS x FROM sales WHERE product_id=? LIMIT 1", (pid,)): raise ApiError("Product has sales records; delete them first.", 409)
        db().exec("DELETE FROM products WHERE product_id=?", (pid,)); db().conn.commit(); return jsonify(deleted=pid)

    # ---- sales ----
    @app.get("/api/sales")
    def sales():
        page, per = max(int(request.args.get("page", 1)), 1), 15
        q = f"%{request.args.get('q', '').strip().lower()}%"
        where = "WHERE LOWER(p.product_name) LIKE ? OR LOWER(s.region) LIKE ?"
        total = db().query(f"SELECT COUNT(*) AS n FROM sales s JOIN products p ON p.product_id=s.product_id {where}", (q, q))[0]["n"]
        rows = db().query(f"SELECT s.sale_id, p.product_name, p.category, s.sale_date, s.quantity, s.unit_price, s.region FROM sales s "
                          f"JOIN products p ON p.product_id=s.product_id {where} ORDER BY s.sale_date DESC, s.sale_id DESC LIMIT ? OFFSET ?",
                          (q, q, per, (page - 1) * per))
        for r in rows: r["revenue"] = round(r["quantity"] * round(r["unit_price"] * 100) / 100, 2)
        return jsonify(rows=rows, total=total, page=page, pages=max(-(-total // per), 1))

    @app.post("/api/sales")
    def add_sale():
        d = request.get_json(silent=True) or {}
        p = db().query("SELECT product_name, category FROM products WHERE product_id=?", (d.get("product_id"),))
        if not p: raise ApiError("Choose a valid product.")
        s = lambda k: "" if d.get(k) is None else str(d.get(k))
        row = pd.DataFrame([dict(**p[0], sale_date=s("sale_date"), quantity=s("quantity"), unit_price=s("unit_price"), region=s("region"), _line=1, _raw="")])
        good, errs = sv.validate(row)
        if errs: raise ApiError("; ".join(e["reason"] for e in errs))
        r = good.iloc[0]   # manual entries get a unique hash: genuine repeat sales are allowed
        sid = db().exec("INSERT INTO sales (product_id, sale_date, quantity, unit_price, region, row_hash) VALUES (?,?,?,?,?,?)",
                        (int(d["product_id"]), r.sale_date, int(r.quantity), float(r.unit_price), r.region, "m" + uuid.uuid4().hex))
        db().conn.commit(); return jsonify(sale_id=sid), 201

    @app.delete("/api/sales/<int:sid>")
    def del_sale(sid): db().exec("DELETE FROM sales WHERE sale_id=?", (sid,)); db().conn.commit(); return jsonify(deleted=sid)

    # ---- analytics ----
    @app.get("/api/filters")
    def filter_options():
        return jsonify(categories=[r["category"] for r in db().query("SELECT DISTINCT category FROM products ORDER BY category")],
                       regions=[r["region"] for r in db().query("SELECT DISTINCT region FROM sales ORDER BY region")])

    @app.get("/api/analytics/summary")
    def a_summary(): return jsonify(sv.summary(sv.load_sales(db(), filters())))
    @app.get("/api/analytics/products")
    def a_products(): return jsonify(sv.group(sv.load_sales(db(), filters()), ["product_name", "category"], request.args.get("by", "revenue")))
    @app.get("/api/analytics/monthly")
    def a_monthly(): return jsonify(sv.monthly(sv.load_sales(db(), filters())))
    @app.get("/api/analytics/categories")
    def a_cat(): return jsonify(sv.group(sv.load_sales(db(), filters()), ["category"]))
    @app.get("/api/analytics/regions")
    def a_reg(): return jsonify(sv.group(sv.load_sales(db(), filters()), ["region"]))

    @app.get("/api/charts/monthly.png")
    def c_monthly(): return Response(sv.monthly_chart(sv.monthly(sv.load_sales(db(), filters()))).getvalue(), mimetype="image/png")
    @app.get("/api/charts/products.png")
    def c_products(): return Response(sv.products_chart(sv.group(sv.load_sales(db(), filters()), ["product_name", "category"], limit=10)).getvalue(), mimetype="image/png")

    # ---- frontend ----
    @app.get("/")
    def index(): return send_from_directory(FRONTEND, "index.html")
    @app.get("/<path:p>")
    def static_files(p): return send_from_directory(FRONTEND, p)
    return app

if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000, debug=False)
