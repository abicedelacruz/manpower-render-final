# app.py
import os
import json
from datetime import datetime, date, time, timedelta

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, abort
)
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from werkzeug.security import generate_password_hash, check_password_hash
import secrets

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", template_folder="templates")

# Read DB URL from environment. Example for Render/Postgres:
#   DATABASE_URL=postgres://user:pass@host:port/dbname
DATABASE_URL = os.environ.get("DATABASE_URL") or "postgresql://postgres:postgres@localhost:5432/abic_payroll"
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-prod")

db = SQLAlchemy(app)
migrate = Migrate(app, db)

# -----------------------------------------------------------------------------
# Models
# -----------------------------------------------------------------------------
class Client(db.Model):
    __tablename__ = "client"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)


class Employee(db.Model):
    __tablename__ = "employee"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("client.id"), nullable=False)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    plain_password = db.Column(db.String(128), nullable=True)  # only for admin display (dev). Remove in prod!
    monthly_salary = db.Column(db.Float, default=0.0)
    rest_day = db.Column(db.String(16), default="Sunday")

    client = db.relationship("Client", backref="employees")


class TimeEntry(db.Model):
    __tablename__ = "time_entry"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    time_in = db.Column(db.Time, nullable=True)
    time_out = db.Column(db.Time, nullable=True)
    # optional tag: 'RD', 'SH', 'RH', 'NONE'
    tag = db.Column(db.String(8), default="NONE")

    employee = db.relationship("Employee", backref="time_entries")


class Payroll(db.Model):
    __tablename__ = "payroll"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    data = db.Column(db.Text, nullable=False)  # JSON summary
    employee = db.relationship("Employee", backref="payrolls")


# -----------------------------------------------------------------------------
# Utilities: credentials, Mali formulas, tax function
# -----------------------------------------------------------------------------
def gen_credentials(name: str):
    username = (''.join(c for c in name.lower() if c.isalnum()) + secrets.token_hex(2))[:12]
    pwd = secrets.token_urlsafe(8)
    return username, pwd


def mali_rates(monthly_salary: float):
    # From your spec:
    basic_pay = monthly_salary / 2.0
    # "daily rate Monthly salary/313*12" — keep formula as given
    daily_rate = (monthly_salary / 313.0) * 12.0
    hourly_rate = daily_rate / 8.0
    return {"basic_pay": basic_pay, "daily_rate": daily_rate, "hourly_rate": hourly_rate}


def compute_income_tax_monthly(taxable: float):
    # Use approximated progressive tax (monthly taxable)
    if taxable <= 20833:
        return 0.0
    if taxable <= 33333:
        return 0.0
    if taxable <= 66667:
        return 0.15 * max(0.0, taxable - 20833)
    if taxable <= 166667:
        return 1875 + 0.20 * (taxable - 33333)
    if taxable <= 666667:
        return 33541.8 + 0.30 * (taxable - 166667)
    return 183541.8 + 0.35 * (taxable - 666667)


def compute_payroll_for_employee(emp: Employee, period_start: date, period_end: date):
    """
    Compute the payroll summary for a single employee using the Mali formulas.
    - 6 days a week, rest_day specified on Employee.
    - scheduled 9:00 -> 18:00 (9 hours, includes 1 hour unpaid break => 8 regular hours).
    - Overtime only counts if time_out > scheduled_out by more than 1 hour (per your rule).
    """
    entries = TimeEntry.query.filter(
        TimeEntry.employee_id == emp.id,
        TimeEntry.date >= period_start,
        TimeEntry.date <= period_end
    ).all()

    rates = mali_rates(emp.monthly_salary or 0.0)
    hourly = rates["hourly_rate"]
    daily = rates["daily_rate"]

    scheduled_in = time(9, 0)
    scheduled_out = time(18, 0)

    total_regular_hours = 0.0
    total_overtime_hours = 0.0
    total_nd_hours = 0.0
    tardiness_hours = 0.0
    undertime_hours = 0.0
    absences = 0

    # iterate dates inclusive
    days_count = (period_end - period_start).days + 1
    for n in range(days_count):
        d = period_start + timedelta(days=n)
        weekday_name = d.strftime("%A")
        is_rest = (weekday_name == emp.rest_day)

        ent = next((e for e in entries if e.date == d), None)

        # If tag indicates holiday/rest day override (admin can set tag on time entry)
        tag = (ent.tag if ent else "NONE")

        if ent is None or ent.time_in is None or ent.time_out is None:
            if not is_rest and tag == "NONE":
                absences += 1
            continue

        tin = datetime.combine(d, ent.time_in)
        tout = datetime.combine(d, ent.time_out)
        if tout < tin:
            # overnight shift
            tout += timedelta(days=1)

        work_dur = (tout - tin).total_seconds() / 3600.0
        # remove unpaid break heuristically
        work_hours = work_dur - 1.0 if work_dur > 5 else work_dur
        work_hours = max(0.0, work_hours)

        # tardiness
        if ent.time_in > scheduled_in and not is_rest:
            tardiness_hours += (datetime.combine(d, ent.time_in) - datetime.combine(d, scheduled_in)).total_seconds() / 3600.0

        # undertime
        if ent.time_out < scheduled_out and not is_rest:
            undertime_hours += (datetime.combine(d, scheduled_out) - datetime.combine(d, ent.time_out)).total_seconds() / 3600.0

        # overtime rule
        overtime = 0.0
        if not is_rest:
            extra = (datetime.combine(d, ent.time_out) - datetime.combine(d, scheduled_out)).total_seconds() / 3600.0
            if extra > 1.0:
                overtime = extra  # count hours beyond scheduled_out if >1.0
        else:
            # any work on rest day considered restday hours (count as overtime-type)
            overtime = work_hours

        # night diff: hours between 22:00 and 06:00
        nd = 0.0
        cursor = tin
        step = timedelta(minutes=15)
        while cursor < tout:
            nxt = min(tout, cursor + step)
            if cursor.time() >= time(22, 0) or cursor.time() < time(6, 0):
                nd += (nxt - cursor).total_seconds() / 3600.0
            cursor = nxt

        regular_h = 0.0 if is_rest else min(8.0, work_hours)
        total_regular_hours += regular_h
        total_overtime_hours += overtime
        total_nd_hours += nd

    # earnings per Mali formulas:
    regular_pay = total_regular_hours * hourly
    ot_pay = total_overtime_hours * hourly * 1.25  # 125%
    rd_pay = 0.0  # can be extended (restday multipliers)
    nd_pay = total_nd_hours * hourly * 1.10

    gross_pay = regular_pay + ot_pay + rd_pay + nd_pay

    # deductions
    sss = emp.monthly_salary * 0.05 if emp.monthly_salary else 0.0
    philhealth = emp.monthly_salary * 0.025 if emp.monthly_salary else 0.0
    pagibig = 200.0

    other_deductions = (undertime_hours * hourly) + (absences * daily)
    taxable_income = gross_pay - (sss + philhealth + pagibig)
    income_tax = compute_income_tax_monthly(taxable_income)
    total_deductions = sss + philhealth + pagibig + other_deductions + income_tax

    net_pay = gross_pay - total_deductions

    summary = {
        "employee_id": emp.id,
        "employee_name": emp.name,
        "period_start": str(period_start),
        "period_end": str(period_end),
        "monthly_salary": emp.monthly_salary,
        "hourly_rate": hourly,
        "daily_rate": daily,
        "total_regular_hours": round(total_regular_hours, 2),
        "total_overtime_hours": round(total_overtime_hours, 2),
        "total_nd_hours": round(total_nd_hours, 2),
        "tardiness_hours": round(tardiness_hours, 2),
        "undertime_hours": round(undertime_hours, 2),
        "absences": absences,
        "regular_pay": round(regular_pay, 2),
        "ot_pay": round(ot_pay, 2),
        "nd_pay": round(nd_pay, 2),
        "gross_pay": round(gross_pay, 2),
        "sss": round(sss, 2),
        "philhealth": round(philhealth, 2),
        "pagibig": round(pagibig, 2),
        "income_tax": round(income_tax, 2),
        "total_deductions": round(total_deductions, 2),
        "net_pay": round(net_pay, 2),
    }
    return summary


# -----------------------------------------------------------------------------
# Routes (Admin + Employee)
# -----------------------------------------------------------------------------
@app.route("/")
def index():
    if "admin" in session:
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "")
        # admin default
        if u == "admin" and p == os.environ.get("ADMIN_PASSWORD", "admin"):
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        # employee login
        emp = Employee.query.filter_by(username=u).first()
        if emp and check_password_hash(emp.password_hash, p):
            session["employee_id"] = emp.id
            return redirect(url_for("employee_dashboard"))
        flash("Invalid credentials", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin")
def admin_dashboard():
    if "admin" not in session:
        return redirect(url_for("login"))
    clients = Client.query.all()
    employees = Employee.query.all()
    payrolls = Payroll.query.order_by(Payroll.created_at.desc()).limit(50).all()
    return render_template("admin_dashboard.html", clients=clients, employees=employees, payrolls=payrolls)


@app.route("/admin/clients/add", methods=["POST"])
def add_client():
    if "admin" not in session:
        return redirect(url_for("login"))
    name = request.form.get("name", "").strip()
    if name:
        if not Client.query.filter_by(name=name).first():
            db.session.add(Client(name=name))
            db.session.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/employees/add", methods=["POST"])
def add_employee():
    if "admin" not in session:
        return redirect(url_for("login"))
    name = request.form.get("name", "").strip()
    client_id = int(request.form.get("client_id"))
    monthly = float(request.form.get("monthly") or 0.0)
    rest_day = request.form.get("rest_day") or "Sunday"
    username, pwd = gen_credentials(name)
    emp = Employee(
        name=name,
        client_id=client_id,
        username=username,
        password_hash=generate_password_hash(pwd),
        plain_password=pwd,
        monthly_salary=monthly,
        rest_day=rest_day,
    )
    db.session.add(emp)
    db.session.commit()
    flash(f"Created employee {name} — username: {username} password: {pwd}", "info")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/employees/<int:emp_id>/timeentries", methods=["GET", "POST"])
def employee_timeentries(emp_id):
    if "admin" not in session:
        return redirect(url_for("login"))
    emp = Employee.query.get_or_404(emp_id)

    if request.method == "POST":
        date_str = request.form.get("date")
        tin = request.form.get("time_in")
        tout = request.form.get("time_out")
        tag = request.form.get("tag") or "NONE"
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            flash("Invalid date format", "danger")
            return redirect(url_for("employee_timeentries", emp_id=emp_id))
        tin_val = datetime.strptime(tin, "%H:%M").time() if tin else None
        tout_val = datetime.strptime(tout, "%H:%M").time() if tout else None
        te = TimeEntry.query.filter_by(employee_id=emp.id, date=d).first()
        if not te:
            te = TimeEntry(employee_id=emp.id, date=d, time_in=tin_val, time_out=tout_val, tag=tag)
            db.session.add(te)
        else:
            te.time_in = tin_val
            te.time_out = tout_val
            te.tag = tag
        db.session.commit()
        flash("Saved time entry", "success")
        return redirect(url_for("employee_timeentries", emp_id=emp_id))

    entries = TimeEntry.query.filter_by(employee_id=emp.id).order_by(TimeEntry.date.desc()).all()
    return render_template("employee_timeentries.html", emp=emp, entries=entries)


@app.route("/admin/employees/<int:emp_id>/generate", methods=["POST"])
def employee_generate_payslip(emp_id):
    if "admin" not in session:
        return redirect(url_for("login"))
    emp = Employee.query.get_or_404(emp_id)
    start = datetime.strptime(request.form.get("period_start"), "%Y-%m-%d").date()
    end = datetime.strptime(request.form.get("period_end"), "%Y-%m-%d").date()
    summary = compute_payroll_for_employee(emp, start, end)
    record = Payroll(employee_id=emp.id, period_start=start, period_end=end, data=json.dumps(summary))
    db.session.add(record)
    db.session.commit()
    flash("Payroll generated", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/timeentries", methods=["GET", "POST"])
def timeentries():
    if "admin" not in session:
        return redirect(url_for("login"))
    employees = Employee.query.all()
    if request.method == "POST":
        emp_id = int(request.form.get("employee_id"))
        date_str = request.form.get("date")
        tin = request.form.get("time_in")
        tout = request.form.get("time_out")
        tag = request.form.get("tag") or "NONE"
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        tin_val = datetime.strptime(tin, "%H:%M").time() if tin else None
        tout_val = datetime.strptime(tout, "%H:%M").time() if tout else None
        te = TimeEntry.query.filter_by(employee_id=emp_id, date=d).first()
        if not te:
            te = TimeEntry(employee_id=emp_id, date=d, time_in=tin_val, time_out=tout_val, tag=tag)
            db.session.add(te)
        else:
            te.time_in = tin_val
            te.time_out = tout_val
            te.tag = tag
        db.session.commit()
        flash("Saved time entry", "success")
        return redirect(url_for("timeentries"))
    entries = TimeEntry.query.order_by(TimeEntry.date.desc()).limit(200).all()
    return render_template("timeentries.html", employees=employees, entries=entries)


@app.route("/admin/payroll/generate", methods=["POST"])
def generate_payroll():
    if "admin" not in session:
        return redirect(url_for("login"))
    emp_id = int(request.form.get("employee_id"))
    start = datetime.strptime(request.form.get("period_start"), "%Y-%m-%d").date()
    end = datetime.strptime(request.form.get("period_end"), "%Y-%m-%d").date()
    emp = Employee.query.get_or_404(emp_id)
    summary = compute_payroll_for_employee(emp, start, end)
    p = Payroll(employee_id=emp.id, period_start=start, period_end=end, data=json.dumps(summary))
    db.session.add(p)
    db.session.commit()
    flash("Payroll generated and saved", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/payroll/<int:pid>")
def view_payroll(pid):
    p = Payroll.query.get_or_404(pid)
    # allow admin or the owner
    if "employee_id" in session and session["employee_id"] != p.employee_id and "admin" not in session:
        flash("Access denied", "danger")
        return redirect(url_for("login"))
    data = json.loads(p.data)
    return render_template("payslip.html", payroll=data, payroll_rec=p)


@app.route("/employee")
def employee_dashboard():
    if "employee_id" not in session:
        return redirect(url_for("login"))
    emp = Employee.query.get_or_404(session["employee_id"])
    payrolls = Payroll.query.filter_by(employee_id=emp.id).order_by(Payroll.created_at.desc()).all()
    return render_template("employee_dashboard.html", emp=emp, payrolls=payrolls)


# a simple health route
@app.route("/health")
def health():
    return {"status": "ok"}


# -----------------------------------------------------------------------------
# DB initialization helper for local dev (safe)
# -----------------------------------------------------------------------------
def seed_default_clients():
    # seed only Mali Lending Corp.
    if not Client.query.filter_by(name="Mali Lending Corp.").first():
        db.session.add(Client(name="Mali Lending Corp."))
        db.session.commit()


@app.cli.command("seed")
def seed_cmd():
    """Run: flask seed  (seeds default client)"""
    seed_default_clients()
    print("Seeded default clients.")


# -----------------------------------------------------------------------------
# Ensure tables exist in local dev (but on Render use migrations)
# -----------------------------------------------------------------------------
@app.before_first_request
def ensure_seed():
    # NOTE: in production we prefer running 'flask db upgrade' via release hook.
    try:
        seed_default_clients()
    except Exception:
        # don't crash the app if db isn't ready here (e.g., before migrations run)
        pass


# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Local dev
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
