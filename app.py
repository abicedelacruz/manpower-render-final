from flask import Flask, render_template, redirect, url_for, request, flash, session, send_from_directory, abort
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, time, timedelta, date
import secrets, math, json, traceback, sys, os

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///payroll.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-in-production')
db = SQLAlchemy(app)

# --------------------
# MODELS
# --------------------
class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, unique=True, nullable=False)

class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String, nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False)
    username = db.Column(db.String, unique=True, nullable=False)
    password_hash = db.Column(db.String, nullable=False)
    plain_password = db.Column(db.String, nullable=True)
    monthly_salary = db.Column(db.Float, default=0.0)
    rest_day = db.Column(db.String, default='Sunday')  # e.g. 'Sunday'
    client = db.relationship('Client')

class TimeEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
    date = db.Column(db.Date, nullable=False)
    time_in = db.Column(db.Time, nullable=True)
    time_out = db.Column(db.Time, nullable=True)
    employee = db.relationship('Employee')

class Payroll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    data = db.Column(db.Text)  # JSON serialized payroll summary
    employee = db.relationship('Employee')

# --------------------
# UTILITIES
# --------------------
def gen_credentials(name):
    username = (''.join(c for c in name.lower() if c.isalnum()) + secrets.token_hex(2))[:12]
    pwd = secrets.token_urlsafe(8)
    return username, pwd

def mali_rates(monthly_salary):
    # formulas provided by user for Mali Lending Corp
    basic_pay = monthly_salary / 2.0
    # Keep the original formula you gave (user's specification)
    daily_rate = monthly_salary / 313.0 * 12.0
    hourly_rate = daily_rate / 8.0
    return {'basic_pay': basic_pay, 'daily_rate': daily_rate, 'hourly_rate': hourly_rate}

def compute_income_tax_monthly(taxable):
    if taxable <= 20833:
        return 0.0
    if taxable <= 33333:
        return 0.0
    if taxable <= 66667:
        return 0.15 * max(0, taxable - 20833)
    if taxable <= 166667:
        return 1875 + 0.20 * (taxable - 33333)
    if taxable <= 666667:
        return 33541.8 + 0.30 * (taxable - 166667)
    return 183541.8 + 0.35 * (taxable - 666667)

def compute_payroll_for_employee(emp, period_start, period_end):
    entries = TimeEntry.query.filter(
        TimeEntry.employee_id == emp.id,
        TimeEntry.date >= period_start,
        TimeEntry.date <= period_end
    ).all()

    r = mali_rates(emp.monthly_salary)
    hourly = r['hourly_rate']
    daily = r['daily_rate']

    total_regular = 0.0
    total_ot = 0.0
    total_nd = 0.0
    tardy = 0.0
    undertime = 0.0
    absences = 0

    sched_in = time(9,0)
    sched_out = time(18,0)

    for d in (period_start + timedelta(days=i) for i in range((period_end - period_start).days + 1)):
        entry = next((e for e in entries if e.date == d), None)
        weekday = d.strftime('%A')
        is_rest = (weekday == emp.rest_day)

        if not entry or not entry.time_in or not entry.time_out:
            if not is_rest:
                absences += 1
            continue

        tin = datetime.combine(d, entry.time_in)
        tout = datetime.combine(d, entry.time_out)
        if tout < tin:
            tout += timedelta(days=1)

        work_hours = (tout - tin).total_seconds() / 3600.0
        if work_hours > 5:
            work_hours -= 1.0

        if entry.time_in > sched_in and not is_rest:
            tardy += (datetime.combine(d, entry.time_in) - datetime.combine(d, sched_in)).seconds / 3600.0

        if entry.time_out < sched_out and not is_rest:
            undertime += (datetime.combine(d, sched_out) - datetime.combine(d, entry.time_out)).seconds / 3600.0

        if not is_rest:
            diff = (tout - datetime.combine(d, sched_out)).total_seconds()/3600.0
            if diff > 1.0:
                total_ot += diff
        else:
            total_ot += work_hours

        # ND calculation (15-min slices)
        nd_hours = 0.0
        cursor = tin
        step = timedelta(minutes=15)
        while cursor < tout:
            nxt = min(tout, cursor + step)
            ht = cursor.time()
            if ht >= time(22,0) or ht < time(6,0):
                nd_hours += (nxt - cursor).total_seconds() / 3600.0
            cursor = nxt

        total_nd += nd_hours
        total_regular += min(8.0, work_hours) if not is_rest else 0.0

    regular_pay = total_regular * hourly
    ot_pay = total_ot * hourly * 1.25
    nd_pay = total_nd * hourly * 1.10
    gross = regular_pay + ot_pay + nd_pay

    sss = emp.monthly_salary * 0.05 if emp.monthly_salary else 0.0
    philhealth = emp.monthly_salary * 0.025 if emp.monthly_salary else 0.0
    pagibig = 200.0

    deductions = sss + philhealth + pagibig + (undertime * hourly) + (absences * daily)
    taxable = gross - (sss + philhealth + pagibig)
    income_tax = compute_income_tax_monthly(taxable)
    deductions += income_tax

    net = gross - deductions

    return {
        "employee_name": emp.name,
        "period_start": str(period_start),
        "period_end": str(period_end),
        "regular_hours": round(total_regular,3),
        "ot_hours": round(total_ot,3),
        "nd_hours": round(total_nd,3),
        "absences": absences,
        "tardiness": round(tardy,3),
        "undertime": round(undertime,3),
        "gross_pay": round(gross,2),
        "net_pay": round(net,2),
        "sss": round(sss,2),
        "philhealth": round(philhealth,2),
        "pagibig": round(pagibig,2),
        "income_tax": round(income_tax,2)
    }

# --------------------
# ROUTES
# --------------------
@app.route('/')
def index():
    if 'admin' in session:
        return redirect(url_for('admin_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        if request.form.get('username') == 'admin' and request.form.get('password') == 'admin':
            session['admin'] = True
            return redirect(url_for('admin_dashboard'))

        emp = Employee.query.filter_by(username=request.form.get('username')).first()
        if emp and check_password_hash(emp.password_hash, request.form.get('password')):
            session['employee_id'] = emp.id
            return redirect(url_for('employee_dashboard'))

        flash('Invalid credentials', 'danger')

    try:
        return render_template('login.html')
    except Exception:
        # If template missing, show helpful info instead of 500
        tb = traceback.format_exc()
        app.logger.error("Template error in login: %s", tb)
        return f"<h3>Login template error</h3><pre>{tb}</pre>", 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
def admin_dashboard():
    if 'admin' not in session:
        return redirect(url_for('login'))
    clients = Client.query.all()
    employees = Employee.query.all()
    payrolls = Payroll.query.order_by(Payroll.created_at.desc()).limit(20).all()
    try:
        return render_template('admin_dashboard.html', clients=clients, employees=employees, payrolls=payrolls)
    except Exception:
        tb = traceback.format_exc()
        app.logger.error("Template error in admin_dashboard: %s", tb)
        return f"<h3>Admin template error</h3><pre>{tb}</pre>", 500

@app.route('/admin/clients/add', methods=['POST'])
def add_client():
    if 'admin' not in session: return redirect(url_for('login'))
    name = request.form.get('name')
    if name:
        c = Client(name=name)
        db.session.add(c); db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/clients/<int:cid>/delete', methods=['POST'])
def delete_client(cid):
    if 'admin' not in session: return redirect(url_for('login'))
    c = Client.query.get_or_404(cid)
    db.session.delete(c); db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/employees/add', methods=['POST'])
def add_employee():
    if 'admin' not in session: return redirect(url_for('login'))
    name = request.form.get('name')
    client_id = int(request.form.get('client_id'))
    monthly = float(request.form.get('monthly') or 0)
    rest_day = request.form.get('rest_day') or 'Sunday'
    username, pwd = gen_credentials(name)
    emp = Employee(
        name=name,
        client_id=client_id,
        username=username,
        password_hash=generate_password_hash(pwd),
        monthly_salary=monthly,
        rest_day=rest_day,
        plain_password=pwd
    )
    db.session.add(emp)
    db.session.commit()
    flash(f'Created employee {name} — username: {username} password: {pwd}', 'info')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/employees/<int:eid>/delete', methods=['POST'])
def delete_employee(eid):
    if 'admin' not in session: return redirect(url_for('login'))
    emp = Employee.query.get_or_404(eid)
    db.session.delete(emp); db.session.commit()
    return redirect(url_for('admin_dashboard'))

# Global time entries (admin)
@app.route('/admin/timeentries', methods=['GET','POST'])
def timeentries():
    if 'admin' not in session: return redirect(url_for('login'))
    employees = Employee.query.all()
    if request.method=='POST':
        try:
            emp_id = int(request.form.get('employee_id'))
            date_str = request.form.get('date')
            tin = request.form.get('time_in')
            tout = request.form.get('time_out')
            d = datetime.strptime(date_str, '%Y-%m-%d').date()
            tin_val = datetime.strptime(tin, '%H:%M').time() if tin else None
            tout_val = datetime.strptime(tout, '%H:%M').time() if tout else None
            te = TimeEntry.query.filter_by(employee_id=emp_id, date=d).first()
            if not te:
                te = TimeEntry(employee_id=emp_id, date=d, time_in=tin_val, time_out=tout_val)
                db.session.add(te)
            else:
                te.time_in = tin_val; te.time_out = tout_val
            db.session.commit()
            flash('Saved time entry','success')
        except Exception as e:
            app.logger.error("Error saving time entry: %s\n%s", e, traceback.format_exc())
            flash('Could not save time entry: ' + str(e), 'danger')
        return redirect(url_for('timeentries'))

    entries = TimeEntry.query.order_by(TimeEntry.date.desc()).limit(50).all()
    try:
        return render_template('timeentries.html', employees=employees, entries=entries)
    except Exception:
        tb = traceback.format_exc()
        app.logger.error("Template error in timeentries: %s", tb)
        return f"<h3>Time entries template error</h3><pre>{tb}</pre>", 500

# Admin-triggered payroll generation (flexible form keys)
@app.route('/admin/payroll/generate', methods=['POST'])
def generate_payroll():
    if 'admin' not in session: return redirect(url_for('login'))
    # accept either 'employee_id' or 'emp_id' and 'period_start' or 'start'
    emp_id = request.form.get('employee_id') or request.form.get('emp_id') or request.form.get('employee')
    if not emp_id:
        flash('Employee not specified', 'danger')
        return redirect(url_for('admin_dashboard'))
    try:
        emp_id = int(emp_id)
        start_val = request.form.get('period_start') or request.form.get('start')
        end_val = request.form.get('period_end') or request.form.get('end')
        start = datetime.strptime(start_val, '%Y-%m-%d').date()
        end = datetime.strptime(end_val, '%Y-%m-%d').date()
    except Exception as e:
        app.logger.error("Error parsing payroll form: %s\n%s", e, traceback.format_exc())
        flash('Invalid payroll dates or employee.', 'danger')
        return redirect(url_for('admin_dashboard'))

    emp = Employee.query.get_or_404(emp_id)
    try:
        summary = compute_payroll_for_employee(emp, start, end)
        p = Payroll(employee_id=emp.id, period_start=start, period_end=end, data=json.dumps(summary))
        db.session.add(p); db.session.commit()
        flash('Payroll generated and saved', 'success')
    except Exception as e:
        app.logger.error("Error generating payroll: %s\n%s", e, traceback.format_exc())
        flash('Could not generate payroll: ' + str(e), 'danger')

    return redirect(url_for('admin_dashboard'))

@app.route('/payroll/<int:pid>')
def view_payroll(pid):
    if 'admin' not in session and 'employee_id' not in session:
        return redirect(url_for('login'))
    p = Payroll.query.get_or_404(pid)
    if 'employee_id' in session and session['employee_id'] != p.employee_id:
        flash('Access denied','danger'); return redirect(url_for('login'))
    try:
        data = json.loads(p.data)
        return render_template('payslip.html', payroll=data, payroll_rec=p)
    except Exception:
        tb = traceback.format_exc()
        app.logger.error("Template or JSON error in view_payroll: %s", tb)
        return f"<h3>Payslip view error</h3><pre>{tb}</pre>", 500

@app.route('/employee')
def employee_dashboard():
    if 'employee_id' not in session: return redirect(url_for('login'))
    emp = Employee.query.get(session['employee_id'])
    payrolls = Payroll.query.filter_by(employee_id=emp.id).order_by(Payroll.created_at.desc()).all()
    try:
        return render_template('employee_dashboard.html', emp=emp, payrolls=payrolls)
    except Exception:
        tb = traceback.format_exc()
        app.logger.error("Template error in employee_dashboard: %s", tb)
        return f"<h3>Employee dashboard error</h3><pre>{tb}</pre>", 500

@app.route('/download/sample_zip')
def download_zip():
    return send_from_directory('/mnt/data', 'abic_payroll_app.zip', as_attachment=True)

# --------------------
# Ensure DB and seed on import (safe for Flask 3+ and Render)
# --------------------
try:
    with app.app_context():
        db.create_all()
        # Keep only Mali Lending Corp as requested
        if not Client.query.filter_by(name='Mali Lending Corp.').first():
            db.session.add(Client(name='Mali Lending Corp.'))
        db.session.commit()
        app.logger.info("Database initialized and Mali Lending Corp seeded.")
except Exception:
    # log exception but don't crash application import
    tb = traceback.format_exc()
    app.logger.error("Error creating DB tables or seeding: %s", tb)

# --------------------
# Per-employee time entries and generate payslip (admin)
# --------------------
@app.route('/admin/employees/<int:emp_id>/timeentries', methods=['GET','POST'])
def employee_timeentries(emp_id):
    if 'admin' not in session:
        return redirect(url_for('login'))
    emp = Employee.query.get_or_404(emp_id)
    if request.method == 'POST':
        date_str = request.form.get('date')
        tin = request.form.get('time_in')
        tout = request.form.get('time_out')
        try:
            d = datetime.strptime(date_str, '%Y-%m-%d').date()
        except Exception:
            flash('Invalid date','danger'); return redirect(url_for('employee_timeentries', emp_id=emp_id))
        tin_val = datetime.strptime(tin, '%H:%M').time() if tin else None
        tout_val = datetime.strptime(tout, '%H:%M').time() if tout else None
        te = TimeEntry.query.filter_by(employee_id=emp.id, date=d).first()
        if not te:
            te = TimeEntry(employee_id=emp.id, date=d, time_in=tin_val, time_out=tout_val)
            db.session.add(te)
        else:
            te.time_in = tin_val; te.time_out = tout_val
        db.session.commit()
        flash('Saved time entry','success')
        return redirect(url_for('employee_timeentries', emp_id=emp_id))

    entries = TimeEntry.query.filter_by(employee_id=emp.id).order_by(TimeEntry.date.desc()).all()
    try:
        return render_template('employee_timeentries.html', emp=emp, entries=entries)
    except Exception:
        tb = traceback.format_exc()
        app.logger.error("Template error in employee_timeentries: %s", tb)
        return f"<h3>Employee timeentries template error</h3><pre>{tb}</pre>", 500

@app.route('/admin/employees/<int:emp_id>/generate', methods=['POST'])
def employee_generate_payslip(emp_id):
    if 'admin' not in session:
        return redirect(url_for('login'))
    emp = Employee.query.get_or_404(emp_id)
    try:
        start = datetime.strptime(request.form.get('period_start'), '%Y-%m-%d').date()
        end = datetime.strptime(request.form.get('period_end'), '%Y-%m-%d').date()
    except Exception as e:
        app.logger.error("Invalid dates for employee-level payroll: %s", e)
        flash('Invalid dates', 'danger')
        return redirect(url_for('employee_timeentries', emp_id=emp_id))
    try:
        summary = compute_payroll_for_employee(emp, start, end)
        p = Payroll(employee_id=emp.id, period_start=start, period_end=end, data=json.dumps(summary))
        db.session.add(p); db.session.commit()
        flash('Payroll generated for ' + emp.name, 'success')
    except Exception as e:
        app.logger.error("Error generating employee payroll: %s\n%s", e, traceback.format_exc())
        flash('Could not generate payroll: ' + str(e), 'danger')
    return redirect(url_for('employee_timeentries', emp_id=emp_id))

# --------------------
# Global error handlers (help debugging on Render)
# --------------------
@app.errorhandler(404)
def not_found(e):
    return render_template('404.html') if os.path.exists(os.path.join(app.template_folder or '', '404.html')) else ("Not Found", 404)

@app.errorhandler(500)
def server_error(e):
    tb = traceback.format_exc()
    app.logger.error("Unhandled exception: %s", tb)
    # show helpful message in browser so you can inspect logs on Render
    return f"<h3>Internal Server Error</h3><pre>{tb}</pre>", 500

# If you run locally with "python app.py" (not required on Render)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
