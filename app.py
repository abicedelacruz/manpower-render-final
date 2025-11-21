
from flask import Flask, render_template, redirect, url_for, request, flash, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, time, timedelta, date
import secrets, math

import json
app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///payroll.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'change-this-secret-in-production'
db = SQLAlchemy(app)

# Models
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

# Utilities
def gen_credentials(name):
    username = (''.join(c for c in name.lower() if c.isalnum()) + secrets.token_hex(2))[:12]
    pwd = secrets.token_urlsafe(8)
    return username, pwd

def mali_rates(monthly_salary):
    # formulas provided by user for Mali Lending Corp
    basic_pay = monthly_salary / 2.0
    daily_rate = monthly_salary / 313.0 * 12.0  # as specified
    hourly_rate = daily_rate / 8.0
    return {'basic_pay': basic_pay, 'daily_rate': daily_rate, 'hourly_rate': hourly_rate}

def compute_payroll_for_employee(emp, period_start, period_end):
    # Load time entries for this period
    entries = TimeEntry.query.filter(TimeEntry.employee_id==emp.id, TimeEntry.date>=period_start, TimeEntry.date<=period_end).all()
    rates = mali_rates(emp.monthly_salary)
    hourly = rates['hourly_rate']
    daily = rates['daily_rate']

    # summary accumulators
    total_regular_hours = 0.0
    total_overtime_hours = 0.0
    total_nd_hours = 0.0
    tardiness_hours = 0.0
    undertime_hours = 0.0
    absences = 0

    # Company schedule: fixed 9am to 6pm, 1 hour unpaid break => count 8 hours regular
    scheduled_in = time(9,0)
    scheduled_out = time(18,0)

    for single_date in (period_start + timedelta(days=n) for n in range((period_end - period_start).days + 1)):
        # find entry for that date
        ent = next((e for e in entries if e.date == single_date), None)
        weekday_name = single_date.strftime('%A')
        is_rest = (weekday_name == emp.rest_day)
        if ent is None or ent.time_in is None or ent.time_out is None:
            # treat as absence if not rest day
            if not is_rest:
                absences += 1
            continue
        # compute work duration in hours (time_out - time_in - 1hr break if crosses midday)
        tin = datetime.combine(single_date, ent.time_in)
        tout = datetime.combine(single_date, ent.time_out)
        if tout < tin:
            # assume overnight -> add a day
            tout += timedelta(days=1)
        work_dur = (tout - tin).total_seconds() / 3600.0
        # remove unpaid break 1 hour if work_dur > 5 (heuristic)
        if work_dur > 5:
            work_hours = max(0.0, work_dur - 1.0)
        else:
            work_hours = work_dur
        # tardiness: minutes late relative to scheduled_in
        if ent.time_in > scheduled_in and not is_rest:
            late = (datetime.combine(single_date, ent.time_in) - datetime.combine(single_date, scheduled_in)).total_seconds() / 3600.0
            tardiness_hours += late
        # undertime: left earlier than scheduled_out (but not rest day), counted in hours
        if ent.time_out < scheduled_out and not is_rest:
            under = (datetime.combine(single_date, scheduled_out) - datetime.combine(single_date, ent.time_out)).total_seconds() / 3600.0
            undertime_hours += under
        # overtime: only when employee time out exceeds scheduled_out by more than 1 hour
        overtime = 0.0
        if not is_rest:
            diff = (datetime.combine(single_date, ent.time_out) - datetime.combine(single_date, scheduled_out)).total_seconds()/3600.0
            if diff > 1.0:
                overtime = diff  # count full hours beyond scheduled_out (per user's rule)
        else:
            # rest day work counts as rest day hours (all work hours)
            overtime = work_hours

        # night differential: any hours worked between 22:00 and 06:00 count as ND
        nd = 0.0
        # break numeric work into 15-minute chunks to detect ND simply
        step = timedelta(minutes=15)
        cursor = tin
        while cursor < tout:
            nxt = min(tout, cursor + step)
            hr = ((nxt - cursor).total_seconds())/3600.0
            htime = cursor.time()
            if (htime >= time(22,0)) or (htime < time(6,0)):
                nd += hr
            cursor = nxt

        # accumulate
        regular_hours = max(0.0, min(8.0, work_hours)) if not is_rest else 0.0
        total_regular_hours += regular_hours
        total_overtime_hours += overtime
        total_nd_hours += nd

    # compute earnings
    regular_pay = total_regular_hours * hourly
    ot_pay = total_overtime_hours * hourly * 1.25  # Regular OT = 125%
    rd_pay = 0.0  # simplified: rest day pay not expanded here
    nd_pay = total_nd_hours * hourly * 1.10  # ND = 110%
    gross_pay = regular_pay + ot_pay + rd_pay + nd_pay
    # deductions (simplified per user)
    sss = emp.monthly_salary * 0.05 if emp.monthly_salary>0 else 0.0
    philhealth = emp.monthly_salary * 0.025 if emp.monthly_salary>0 else 0.0
    pagibig = 200.0
    total_deductions = sss + philhealth + pagibig + (undertime_hours * hourly) + (absences * daily)
    taxable_income = gross_pay - (sss + philhealth + pagibig)
    # simplified income tax using provided brackets (monthly amounts)
    income_tax = compute_income_tax_monthly(taxable_income)
    total_deductions += income_tax
    net_pay = gross_pay - total_deductions

    summary = {
        'employee_id': emp.id,
        'employee_name': emp.name,
        'period_start': str(period_start),
        'period_end': str(period_end),
        'monthly_salary': emp.monthly_salary,
        'hourly_rate': hourly,
        'total_regular_hours': total_regular_hours,
        'total_overtime_hours': total_overtime_hours,
        'total_nd_hours': total_nd_hours,
        'tardiness_hours': tardiness_hours,
        'undertime_hours': undertime_hours,
        'absences': absences,
        'regular_pay': round(regular_pay,2),
        'ot_pay': round(ot_pay,2),
        'nd_pay': round(nd_pay,2),
        'gross_pay': round(gross_pay,2),
        'sss': round(sss,2),
        'philhealth': round(philhealth,2),
        'pagibig': round(pagibig,2),
        'income_tax': round(income_tax,2),
        'total_deductions': round(total_deductions,2),
        'net_pay': round(net_pay,2)
    }
    return summary

def compute_income_tax_monthly(taxable):
    # Use the bracket table provided by user. The table is ambiguous for some ranges; implement a commonly used PH tax (approx)
    # For simplicity, use progressive bracket per monthly taxable income:
    # 0 - 20,833 : 0
    # 20,833 - 33,333 : 0%? (user had incomplete rows). We'll implement approximate 15% on excess over 20,833 for >66,667 per user's later lines.
    if taxable <= 20833:
        return 0.0
    # We'll implement simple progressive mapping approximating the given rules:
    if taxable <= 33333:
        # approximate 0% as per one line; use 0 for lower bracket
        return 0.0
    if taxable <= 66667:
        # 15% of excess over 20833? user text ambiguous; use 0.15*(taxable-20833)
        return 0.15 * max(0, taxable - 20833)
    if taxable <= 166667:
        return 1875 + 0.20 * (taxable - 33333)
    if taxable <= 666667:
        return 33541.8 + 0.30 * (taxable - 166667)
    # over 666667
    return 183541.8 + 0.35 * (taxable - 666667)

# Routes: simple admin login (no separate user table for admin in this prototype)
@app.route('/')
def index():
    if 'admin' in session:
        return redirect(url_for('admin_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        if request.form.get('username')=='admin' and request.form.get('password')=='admin':
            session['admin'] = True
            return redirect(url_for('admin_dashboard'))
        # employee login
        emp = Employee.query.filter_by(username=request.form.get('username')).first()
        if emp and check_password_hash(emp.password_hash, request.form.get('password')):
            session['employee_id'] = emp.id
            return redirect(url_for('employee_dashboard'))
        flash('Invalid credentials','danger')
    return render_template('login.html')

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
    return render_template('admin_dashboard.html', clients=clients, employees=employees, payrolls=payrolls)

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
    emp = Employee(name=name, client_id=client_id, username=username, password_hash=generate_password_hash(pwd), monthly_salary=monthly, rest_day=rest_day, plain_password=pwd)
    db.session.add(emp); db.session.commit()
    flash(f'Created employee {name} — username: {username} password: {pwd}', 'info')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/employees/<int:eid>/delete', methods=['POST'])
def delete_employee(eid):
    if 'admin' not in session: return redirect(url_for('login'))
    emp = Employee.query.get_or_404(eid)
    db.session.delete(emp); db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/timeentries', methods=['GET','POST'])
def timeentries():
    if 'admin' not in session: return redirect(url_for('login'))
    employees = Employee.query.all()
    if request.method=='POST':
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
        return redirect(url_for('timeentries'))
    entries = TimeEntry.query.order_by(TimeEntry.date.desc()).limit(50).all()
    return render_template('timeentries.html', employees=employees, entries=entries)

@app.route('/admin/payroll/generate', methods=['POST'])
def generate_payroll():
    if 'admin' not in session: return redirect(url_for('login'))
    emp_id = int(request.form.get('employee_id'))
    start = datetime.strptime(request.form.get('period_start'), '%Y-%m-%d').date()
    end = datetime.strptime(request.form.get('period_end'), '%Y-%m-%d').date()
    emp = Employee.query.get_or_404(emp_id)
    summary = compute_payroll_for_employee(emp, start, end)
    p = Payroll(employee_id=emp.id, period_start=start, period_end=end, data=json.dumps(summary))
    db.session.add(p); db.session.commit()
    flash('Payroll generated and saved','success')
    return redirect(url_for('admin_dashboard'))

@app.route('/payroll/<int:pid>')
def view_payroll(pid):
    if 'admin' not in session and 'employee_id' not in session:
        return redirect(url_for('login'))
    p = Payroll.query.get_or_404(pid)
    # ensure employee can only view their own
    if 'employee_id' in session and session['employee_id'] != p.employee_id:
        flash('Access denied','danger'); return redirect(url_for('login'))
    data = json.loads(p.data)
    return render_template('payslip.html', payroll=data, payroll_rec=p)

@app.route('/employee')
def employee_dashboard():
    if 'employee_id' not in session: return redirect(url_for('login'))
    emp = Employee.query.get(session['employee_id'])
    payrolls = Payroll.query.filter_by(employee_id=emp.id).order_by(Payroll.created_at.desc()).all()
    return render_template('employee_dashboard.html', emp=emp, payrolls=payrolls)

# Simple static file serving for zipped demo
@app.route('/download/sample_zip')
def download_zip():
    return send_from_directory('/mnt/data', 'abic_payroll_app.zip', as_attachment=True)



# ------------------
# Ensure DB and seed on start (Render-friendly)
# ------------------
@app.before_first_request
def initialize_database():
    with app.app_context():
        db.create_all()

        # Seed only Mali Lending Corp.
        default_clients = ['Mali Lending Corp.']
        for c in default_clients:
            if not Client.query.filter_by(name=c).first():
                db.session.add(Client(name=c))
        db.session.commit()



# ------------------
# Per-employee time entries and generate payslip (admin)
# ------------------
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
        except:
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
    return render_template('employee_timeentries.html', emp=emp, entries=entries)


@app.route('/admin/employees/<int:emp_id>/generate', methods=['POST'])
def employee_generate_payslip(emp_id):
    if 'admin' not in session:
        return redirect(url_for('login'))
    emp = Employee.query.get_or_404(emp_id)
    start = datetime.strptime(request.form.get('period_start'), '%Y-%m-%d').date()
    end = datetime.strptime(request.form.get('period_end'), '%Y-%m-%d').date()
    summary = compute_payroll_for_employee(emp, start, end)
    p = Payroll(employee_id=emp.id, period_start=start, period_end=end, data=json.dumps(summary))
    db.session.add(p); db.session.commit()
    flash('Payroll generated for ' + emp.name, 'success')
    return redirect(url_for('admin_dashboard'))

