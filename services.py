"""CSV validation, import (with duplicate policy), analytics (pandas/NumPy) and charts (Matplotlib)."""
import csv, hashlib, io, json
from datetime import date, datetime
import numpy as np, pandas as pd
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter

REQUIRED = ["product_name", "category", "sale_date", "quantity", "unit_price", "region"]
FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")

def make_hash(name, d, q, p, region):
    """Duplicate key = product + date + quantity + price + region (case-insensitive)."""
    return hashlib.sha1(f"{name.lower()}|{d}|{int(q)}|{p:.2f}|{region.lower()}".encode()).hexdigest()

def parse_csv(data):
    """Returns (frame, structural_errors, total_rows, fatal_message)."""
    try: text = data.decode("utf-8-sig")
    except UnicodeDecodeError: return None, [], 0, "File is not valid UTF-8 text."
    header, rows, errors, total = None, [], [], 0
    try:
        rd = csv.reader(io.StringIO(text, newline=""))
        for row in rd:
            if not any(c.strip() for c in row): continue
            if header is None:
                header = [c.strip().lower().replace(" ", "_") for c in row]; continue
            total += 1
            if len(row) != len(header):
                errors.append(dict(line=rd.line_num, type="malformed", row=",".join(row),
                                   reason=f"Expected {len(header)} columns but found {len(row)}"))
            else: rows.append(row + [rd.line_num, ",".join(row)])
    except csv.Error as e: return None, [], 0, f"CSV could not be read: {e}"
    if header is None: return None, [], 0, "File is empty."
    missing = [c for c in REQUIRED if c not in header]
    if missing: return None, [], 0, "Missing required column(s): " + ", ".join(missing)
    if len(set(header)) != len(header): return None, [], 0, "Duplicate column names in header."
    df = pd.DataFrame(rows, columns=header + ["_line", "_raw"], dtype=str)
    df["_line"] = df["_line"].astype(int)
    return df, errors, total, None

def validate(df):
    """Vectorised row validation/cleaning. Returns (clean_rows, error_list)."""
    df = df.copy()
    for c in REQUIRED: df[c] = df[c].astype(str).str.strip()
    df["product_name"] = df.product_name.str.replace(r"\s+", " ", regex=True)
    q = pd.to_numeric(df.quantity.str.replace(",", "", regex=False), errors="coerce")
    p = pd.to_numeric(df.unit_price.str.replace(r"^[₹$€£]\s*|,", "", regex=True), errors="coerce")
    q, p = q.where(np.isfinite(q)), p.where(np.isfinite(p))
    d = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
    for f in FORMATS: d = d.fillna(pd.to_datetime(df.sale_date, format=f, errors="coerce"))
    checks = [(df.product_name == "", "product_name is empty"),
              (df.product_name.str.len() > 150, "product_name is longer than 150 characters"),
              (df.category == "", "category is empty"), (df.region == "", "region is empty"),
              (d.isna(), "sale_date is missing or invalid (use YYYY-MM-DD)"),
              (d > pd.Timestamp(date.today()), "sale_date is in the future"),
              (q.isna() | (q != np.floor(q)), "quantity must be a whole number"),
              (q <= 0, "quantity must be greater than 0"),
              (p.isna(), "unit_price must be a number"), (p < 0, "unit_price cannot be negative")]
    reasons = pd.Series("", index=df.index)
    for mask, msg in checks: reasons = reasons.where(~mask, reasons + msg + "; ")
    bad = reasons != ""
    errors = [dict(line=int(l), type="invalid", reason=r.rstrip("; "), row=raw)
              for l, r, raw in zip(df._line[bad], reasons[bad], df._raw[bad])]
    g = df[~bad].copy()
    g["sale_date"] = d[~bad].dt.strftime("%Y-%m-%d")
    g["quantity"], g["unit_price"] = q[~bad].astype(int), p[~bad].round(2)
    g["category"], g["region"] = g.category.str.title(), g.region.str.title()
    g["row_hash"] = [make_hash(*t) for t in zip(g.product_name, g.sale_date, g.quantity, g.unit_price, g.region)]
    return g, errors

def import_csv(db, filename, data):
    """Duplicate policy: a row whose product+date+quantity+price+region already exists
    (earlier in the file OR from any previous import) is skipped and reported, never inserted twice."""
    frame, report, total, fatal = parse_csv(data)
    if fatal: return {"fatal": fatal}
    good, invalid = validate(frame)
    report += invalid
    dup = good.duplicated("row_hash", keep="first")
    for _, r in good[dup].iterrows():
        report.append(dict(line=int(r._line), type="duplicate_in_file", row=r._raw,
                           reason="Same product/date/quantity/price/region appears earlier in this file"))
    good = good[~dup]
    existing = set()
    hashes = list(good.row_hash)
    for i in range(0, len(hashes), 500):
        chunk = hashes[i:i + 500]
        existing |= {r["row_hash"] for r in db.query(
            f"SELECT row_hash FROM sales WHERE row_hash IN ({','.join('?' * len(chunk))})", chunk)}
    seen = good.row_hash.isin(existing)
    for _, r in good[seen].iterrows():
        report.append(dict(line=int(r._line), type="duplicate_existing", row=r._raw,
                           reason="Identical sale already exists in the database (previous import)"))
    new = good[~seen]
    report.sort(key=lambda e: e["line"])
    n_err = sum(e["type"] in ("malformed", "invalid") for e in report)
    n_dup = len(report) - n_err
    try:
        imp = db.exec("INSERT INTO import_logs (filename, imported_at, total_rows, inserted_rows, duplicate_rows, error_rows, error_report) VALUES (?,?,?,?,?,?,?)",
                      (filename, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), total, len(new), n_dup, n_err, json.dumps(report[:5000])))
        pmap = {r["product_name"].lower(): r["product_id"] for r in db.query("SELECT product_id, product_name FROM products")}
        for name, cat in zip(new.product_name, new.category):
            if name.lower() not in pmap:
                pmap[name.lower()] = db.exec("INSERT INTO products (product_name, category) VALUES (?,?)", (name, cat))
        db.exec("INSERT INTO sales (product_id, sale_date, quantity, unit_price, region, row_hash, import_id) VALUES (?,?,?,?,?,?,?)",
                [(pmap[n.lower()], d, int(q), float(p), r, h, imp) for n, d, q, p, r, h in
                 zip(new.product_name, new.sale_date, new.quantity, new.unit_price, new.region, new.row_hash)], many=True)
        db.conn.commit()
    except Exception:
        db.conn.rollback(); raise
    return dict(import_id=imp, filename=filename, total_rows=total, inserted=len(new), duplicates=n_dup,
                errors=n_err, details=report[:200], message=f"{len(new)} of {total} rows imported.")

# ---------------- analytics ----------------
def load_sales(db, f):
    sql = ("SELECT s.sale_id, s.product_id, p.product_name, p.category, s.sale_date, s.quantity, s.unit_price, s.region "
           "FROM sales s JOIN products p ON p.product_id = s.product_id WHERE 1=1")
    params = []
    for col, key, op in (("s.sale_date", "start", ">="), ("s.sale_date", "end", "<="), ("p.category", "category", "="), ("s.region", "region", "=")):
        if f.get(key): sql += f" AND {col} {op} ?"; params.append(f[key])
    cols = ["sale_id", "product_id", "product_name", "category", "sale_date", "quantity", "unit_price", "region"]
    df = pd.DataFrame(db.query(sql, params), columns=cols)
    df["sale_date"] = pd.to_datetime(df.sale_date)
    df["quantity"] = df.quantity.astype("int64")
    df["revenue_cents"] = df.quantity * np.round(df.unit_price.astype(float) * 100).astype("int64")  # exact integer money
    df["month"] = df.sale_date.dt.strftime("%Y-%m")
    return df

def _money(c): return round(float(c) / 100, 2)

def group(df, cols, by="revenue", limit=None):
    if df.empty: return []
    g = df.groupby(cols, as_index=False).agg(quantity=("quantity", "sum"), revenue_cents=("revenue_cents", "sum"), transactions=("sale_id", "count"))
    total = g.revenue_cents.sum()
    g["share_pct"] = np.round(g.revenue_cents / total * 100, 2) if total else 0.0
    g = g.sort_values(["revenue_cents" if by == "revenue" else "quantity"] + cols[:1], ascending=[False] + [True])
    g["rank"] = np.arange(1, len(g) + 1)
    g["revenue"] = g.revenue_cents.map(_money)
    out = g.drop(columns="revenue_cents").head(limit or len(g))
    return json.loads(out.to_json(orient="records"))

def monthly(df):
    if df.empty: return []
    m = df.groupby("month").agg(quantity=("quantity", "sum"), revenue_cents=("revenue_cents", "sum"), transactions=("sale_id", "count"))
    m = m.reindex(pd.period_range(m.index.min(), m.index.max(), freq="M").strftime("%Y-%m"), fill_value=0)
    rev = m.revenue_cents.to_numpy(float)
    growth = np.full(len(rev), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        growth[1:] = np.where(rev[:-1] > 0, (rev[1:] - rev[:-1]) / rev[:-1] * 100, np.nan)
    return [dict(month=k, quantity=int(r.quantity), revenue=_money(r.revenue_cents), transactions=int(r.transactions),
                 growth_pct=None if np.isnan(gr) else round(float(gr), 2)) for (k, r), gr in zip(m.iterrows(), growth)]

def summary(df):
    if df.empty: return dict(total_revenue=0, total_quantity=0, transactions=0, avg_sale_value=0, top_product=None, top_product_by_quantity=None, best_month=None)
    total = int(df.revenue_cents.sum()); mo = monthly(df)
    best = max(mo, key=lambda m: m["revenue"])
    return dict(total_revenue=_money(total), total_quantity=int(df.quantity.sum()), transactions=len(df),
                avg_sale_value=_money(total / len(df)), top_product=group(df, ["product_name", "category"])[0]["product_name"],
                top_product_by_quantity=group(df, ["product_name", "category"], "quantity")[0]["product_name"],
                best_month=best["month"], best_month_revenue=best["revenue"],
                first_date=df.sale_date.min().strftime("%Y-%m-%d"), last_date=df.sale_date.max().strftime("%Y-%m-%d"))

# ---------------- charts ----------------
def _png(fig):
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=110, bbox_inches="tight"); buf.seek(0); return buf

def _empty(fig): fig.text(0.5, 0.5, "No sales data for the selected filters", ha="center"); return _png(fig)

def monthly_chart(mo):
    fig = Figure(figsize=(9, 6))
    if not mo: return _empty(fig)
    x = [m["month"] for m in mo]
    a1, a2 = fig.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    a1.plot(x, [m["revenue"] for m in mo], marker="o", color="#2563eb"); a1.set_title("Monthly revenue"); a1.grid(alpha=.3)
    a1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    a2.bar(x, [m["quantity"] for m in mo], color="#16a34a"); a2.set_title("Units sold per month"); a2.grid(alpha=.3, axis="y")
    for l in a2.get_xticklabels(): l.set_rotation(45); l.set_ha("right")
    fig.tight_layout(); return _png(fig)

def products_chart(rows):
    fig = Figure(figsize=(9, 5)); ax = fig.subplots()
    if not rows: return _empty(fig)
    rows = rows[::-1]
    ax.barh([r["product_name"] for r in rows], [r["revenue"] for r in rows], color="#f59e0b")
    ax.set_title("Top products by revenue"); ax.grid(alpha=.3, axis="x")
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    fig.tight_layout(); return _png(fig)
