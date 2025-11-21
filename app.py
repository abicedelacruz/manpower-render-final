# app.py
from flask import Flask, render_template, redirect, url_for, request, flash, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, time, timedelta, date
import secrets, math, json

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///payroll.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'change-this-secret-in-production'
db = SQLAlchemy(app)

# ---------------------------
# MODELS
# ---------------------------
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
    # optional label managed automatically by system (RD/REG/SPEC/REGH)
    employee = db.relationship('Employee')

class Holiday(db.Model):
    """
    A holiday record. admin can add a date and type:
      - type = 'REGULAR' (Regular holiday)
      - type = 'SPECIAL' (Special holiday)
    The system will automatically consult this table to determine day type.
    """
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, unique=True, nullable=False)
    type = db.Column(db.String, nullable=False)  # 'REGULAR' or 'SPECIAL'
    note = db.Column(db.String, nullable=True)

class Payroll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey('employee.id'), nullable=False)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    data = db.Column(db.Text)  # JSON serialized payroll summary
    employee = db.relationship('Employee')

# ---------------------------
# Utilities & Rates (per Excel)
# ---------------------------
def gen_credentials(name):
    username = (''.join(c for c in name.lower() if c.isalnum()) + secrets.token_hex(2))[:12]
    pwd = secrets.token_urlsafe(8)
    return username, pwd

def basic_and_rates_from_monthly(monthly_salary):
    """
    Returns basic_pay, daily_rate, hourly_rate according to:
      Basic pay = monthly / 2
      Daily rate = Monthly salary / 313 * 12
      Hourly = daily / 8
    (Using the exact formulas you provided)
    """
    basic_pay = monthly_salary / 2.0
    daily_rate = monthly_salary / 313.0 * 12.0
    hourly_rate = daily_rate / 8.0
    return basic_pay, daily_rate, hourly_rate

# multipliers from your spreadsheet
# We'll compute final chunk multiplier as: base_multiplier * (1.10 if ND else 1.0)
# base multipliers (non-ND)
BASE = {
    'REGULAR': 1.00,
    'REGULAR_OT': 1.25,
    'RESTDAY': 1.30,
    'RESTDAY_OT': 1.69,
    'SPECIAL': 1.30,
    'SPECIAL_OT': 1.69,
    'SPECIAL_ON_RD': 1.50,
    'SPECIAL_ON_RD_OT': 1.69,
    'REGULAR_HOL': 2.00,
    'REGULAR_HOL_OT': 2.60,
    'REGULAR_HOL_ON_RD': 2.60,
}
ND_FACTOR = 1.10  # Night Diff 110%
# Combined ND OT multipliers are achieved by multiplying base * 1.10 (e.g., 1.25*1.10=1.375)

def get_holiday_type_for_date(d: date):
    """Return 'REGULAR' or 'SPECIAL' or None based on Holiday table."""
    h = Holiday.query.filter_by(date=d).first()
    return h.type if h else None

def is_nd_time(dt_time: time):
    """Night diff between 22:00 - 06:00"""
    return (dt_time >= time(22,0)) or (dt_time < time(6,0))

# ---------------------------
# New robust payroll computation (rewrite)
# ---------------------------
def compute_payroll_for_employee(emp, period_start, period_end):
    """
    Rewritten payroll calculation to follow the exact multipliers in your spreadsheet.
    Strategy:
      - For each day in the period, read TimeEntry (time_in/time_out).
      - For each worked day, split the worked period into 15-minute chunks.
      - For each chunk determine:
          * is_rest_day (employee.rest_day)
          * holiday_type (Holiday table: REGULAR or SPECIAL)
          * is_ot : for normal (non-RD) day OT = time > scheduled_out + 1hr (i.e. after 19:00)
                   for restday: we treat the first 8 worked hours as RESTDAY (1.30), hours beyond 8 as RESTDAY_OT (1.69)
          * is_nd : time between 22:00 - 06:00
      - For each chunk compute multiplier using the table:
          final_multiplier = base_multiplier * (ND_FACTOR if is_nd else 1.0)
      - Sum payment: pay += hourly_rate * final_multiplier * chunk_hours
      - Also track totals (regular hours, ot hours, nd hours, tardiness minutes, undertime minutes, absences)
    """

    # rates & basics
    basic_pay, daily_rate, hourly_rate = basic_and_rates_from_monthly(emp.monthly_salary)

    # scheduled times
    scheduled_in = time(9,0)
    scheduled_out = time(18,0)
    # OT only when exceeded 1 hour after scheduled_out => after 19:00
    scheduled_ot_start = (datetime.combine(date.today(), scheduled_out) + timedelta(hours=1)).time()

    # accumulators
    totals = {
        'chunk_pay': 0.0,
        'regular_hours': 0.0,
        'ot_hours': 0.0,
        'nd_hours': 0.0,
        'tardiness_minutes': 0.0,
        'undertime_minutes': 0.0,
        'absences': 0,
        'days_worked': 0
    }

    # Load entries once
    entries = TimeEntry.query.filter(
        TimeEntry.employee_id==emp.id,
        TimeEntry.date>=period_start,
        TimeEntry.date<=period_end
    ).all()

    # iterate day by day
    day_count = (period_end - period_start).days + 1
    for i in range(day_count):
        cur_date = period_start + timedelta(days=i)
        weekday_name = cur_date.strftime('%A')
        is_rest = (weekday_name == emp.rest_day)
        holiday_type = get_holiday_type_for_date(cur_date)  # 'REGULAR', 'SPECIAL', or None

        # find time entry
        ent = next((e for e in entries if e.date == cur_date), None)
        if ent is None or ent.time_in is None or ent.time_out is None:
            if not is_rest and holiday_type is None:
                totals['absences'] += 1
            continue

        totals['days_worked'] += 1

        # combine datetimes
        tin = datetime.combine(cur_date, ent.time_in)
        tout = datetime.combine(cur_date, ent.time_out)
        if tout < tin:
            tout += timedelta(days=1)

        # total worked hours for the day (apply unpaid 1 hour break if >5 hours)
        work_dur_hours = (tout - tin).total_seconds() / 3600.0
        work_hours_effective = work_dur_hours - 1.0 if work_dur_hours > 5 else work_dur_hours
        if work_hours_effective < 0:
            work_hours_effective = 0.0

        # tardiness & undertime minutes
        if ent.time_in > scheduled_in and not is_rest and holiday_type is None:
            tard_delta = datetime.combine(cur_date, ent.time_in) - datetime.combine(cur_date, scheduled_in)
            totals['tardiness_minutes'] += tard_delta.total_seconds() / 60.0
        if ent.time_out < scheduled_out and not is_rest and holiday_type is None:
            under_delta = datetime.combine(cur_date, scheduled_out) - datetime.combine(cur_date, ent.time_out)
            totals['undertime_minutes'] += under_delta.total_seconds() / 60.0

        # For rest day OT definition: we'll treat the first 8 effective hours as RESTDAY
        # and any hours beyond 8 as RESTDAY_OT.
        restday_hours_counted = 0.0

        # iterate in 15-minute chunks
        step = timedelta(minutes=15)
        cursor = tin
        while cursor < tout:
            nxt = min(tout, cursor + step)
            chunk_hours = (nxt - cursor).total_seconds() / 3600.0
            chunk_time = cursor.time()

            # ND detection
            nd = is_nd_time(chunk_time)

            # detect OT for non-rest days: after scheduled_out + 1 hour
            ot = False
            if not is_rest and holiday_type is None:
                if chunk_time > scheduled_ot_start:
                    ot = True
            # if it's a regular holiday/day off or rest day, we'll handle using rest/holiday logic below

            # Base multiplier logic (non-ND)
            base_mult = 1.0

            if holiday_type == 'REGULAR':
                # regular holiday
                if ot:
                    base_mult = BASE['REGULAR_HOL_OT']
                else:
                    base_mult = BASE['REGULAR_HOL']
            elif holiday_type == 'SPECIAL':
                # special holiday - if rest day as well
                if is_rest:
                    # special holiday on rest day
                    # per table: Special holiday on RD = 150% for simple hours
                    # and OT on such day uses 169% (per your sheet groupings)
                    if ot:
                        base_mult = BASE['SPECIAL_ON_RD_OT']
                    else:
                        base_mult = BASE['SPECIAL_ON_RD']
                else:
                    if ot:
                        base_mult = BASE['SPECIAL_OT']
                    else:
                        base_mult = BASE['SPECIAL']
            elif is_rest:
                # rest day (not holiday)
                # treat first 8 hours as RESTDAY, after that as RESTDAY_OT
                if restday_hours_counted >= 8.0:
                    base_mult = BASE['RESTDAY_OT']
                    ot = True  # considered OT for rest day after 8 hours
                else:
                    # if adding chunk exceeds 8h boundary, split: but we keep simple: if current chunk pushes beyond 8h,
                    # mark proportion accordingly by splitting chunk (rare with 15-min step; below we approximate)
                    remain_before_8 = max(0.0, 8.0 - restday_hours_counted)
                    if chunk_hours <= remain_before_8:
                        base_mult = BASE['RESTDAY']
                    else:
                        # split chunk into two portions: portion_before (remain_before_8) and portion_after (rest)
                        # to be accurate, we'll prorate current chunk accordingly
                        portion_before = remain_before_8
                        portion_after = chunk_hours - portion_before
                        # pay the before and after separately:
                        # before chunk
                        pay_before = hourly_rate * BASE['RESTDAY'] * portion_before
                        # after chunk => RESTDAY_OT
                        pay_after = hourly_rate * BASE['RESTDAY_OT'] * portion_after
                        if nd:
                            pay_before *= ND_FACTOR
                            pay_after *= ND_FACTOR
                        totals['chunk_pay'] += pay_before + pay_after
                        totals['regular_hours'] += portion_before
                        totals['ot_hours'] += portion_after
                        if nd:
                            totals['nd_hours'] += chunk_hours
                        restday_hours_counted += chunk_hours
                        cursor = nxt
                        continue
                restday_hours_counted += chunk_hours
            else:
                # normal (non-holiday, non-rest) day
                if ot:
                    base_mult = BASE['REGULAR_OT']
                else:
                    base_mult = BASE['REGULAR']

            # final multiplier includes ND factor (multiply)
            final_mult = base_mult * (ND_FACTOR if nd else 1.0)

            # compute pay for this chunk
            chunk_pay = hourly_rate * final_mult * chunk_hours
            totals['chunk_pay'] += chunk_pay

            # classify hours for summary
            # If base_mult is >1 and corresponds to OT categories we count them as OT hours
            if base_mult in (BASE['REGULAR_OT'], BASE['RESTDAY_OT'], BASE['SPECIAL_OT'], BASE['SPECIAL_ON_RD_OT'], BASE['REGULAR_HOL_OT']):
                totals['ot_hours'] += chunk_hours
            else:
                totals['regular_hours'] += chunk_hours

            if nd:
                totals['nd_hours'] += chunk_hours

            cursor = nxt

    # totals -> compute gross/deductions/tax/net
    gross_pay = totals['chunk_pay']
    # compute statutory deductions
    sss = emp.monthly_salary * 0.05 if emp.monthly_salary else 0.0
    philhealth = emp.monthly_salary * 0.025 if emp.monthly_salary else 0.0
    pagibig = 200.0

    # lateness & undertime deductions (convert minutes to hours * hourly_rate)
    late_ded = (totals['tardiness_minutes'] / 60.0) * hourly_rate
    undertime_ded = (totals['undertime_minutes'] / 60.0) * hourly_rate
    lwop_ded = totals['absences'] * daily_rate

    total_deductions = sss + philhealth + pagibig + late_ded + undertime_ded + lwop_ded

    # taxable income (as per your sheet)
    taxable_income = gross_pay - (sss + philhealth + pagibig)

    # income tax using the brackets you specified (exact mapping)
    income_tax = compute_income_tax_monthly_from_table(taxable_income)

    total_deductions += income_tax
    net_pay = gross_pay - total_deductions

    result = {
        'employee_id': emp.id,
        'employee_name': emp.name,
        'period_start': str(period_start),
        'period_end': str(period_end),
        'monthly_salary': emp.monthly_salary,
        'basic_pay': round(basic_pay,2),
        'daily_rate': round(daily_rate,2),
        'hourly_rate': round(hourly_rate,2),
        'regular_hours': round(totals['regular_hours'],2),
        'ot_hours': round(totals['ot_hours'],2),
        'nd_hours': round(totals['nd_hours'],2),
        'days_worked': totals['days_worked'],
        'tardiness_minutes': round(totals['tardiness_minutes'],2),
        'undertime_minutes': round(totals['undertime_minutes'],2),
        'absences': totals['absences'],
        'gross_pay': round(gross_pay,2),
        'sss': round(sss,2),
        'philhealth': round(philhealth,2),
        'pagibig': round(pagibig,2),
        'late_deduction': round(late_ded,2),
        'undertime_deduction': round(undertime_ded,2),
        'lwop_deduction': round(lwop_ded,2),
        'income_tax': round(income_tax,2),
        'total_deductions': round(total_deductions,2),
        'net_pay': round(net_pay,2)
    }

    return result

def compute_income_tax_monthly_from_table(taxable):
    """Income tax mapping to match the spreadsheet table (monthly taxable income)."""
    # The sheet's brackets are a bit ambiguous; implement common PH progressive mapping per your table:
    # 0 - 20,833 : 0
    # 20,833 - 33,333 : 0
    # 33,333 - 66,667 : 0% (No tax) ??? (sheet ambiguous) - we will follow the later lines:
    # For safety we implement progressive as:
    if taxable <= 20833:
        return 0.0
    if taxable <= 33333:
        return 0.0
    if taxable <= 66667:
        # 15% of excess over 20,833 (as in your prior attempt)
        return 0.15 * max(0.0, taxable - 20833)
    if taxable <= 166667:
        return 1875 + 0.20 * (taxable - 33333)
    if taxable <= 666667:
        return 33541.8 + 0.30 * (taxable - 166667)
    return 183541.8 + 0.35 * (taxable - 666667)

# ---------------------------
# ROUTES (admin / employee)
# ---------------------------
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
    holidays = Holiday.query.order_by(Holiday.date.desc()).all()
    return render_template('admin_dashboard.html', clients=clients, employees=employees, payrolls=payrolls, holidays=holidays)

# client CRUD
@app.route('/admin/clients/add', methods=['POST'])
def add_client():
    if 'admin' not in session: return redirect(url_for('login'))
    name = request.form.get('name')
    if name:
        db.session.add(Client(name=name))
        db.session.commit()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/clients/<int:cid>/delete', methods=['POST'])
def delete_client(cid):
    if 'admin' not in session: return redirect(url_for('login'))
    c = Client.query.get_or_404(cid)
    db.session.delete(c); db.session.commit()
    return redirect(url_for('admin_dashboard'))

# employee CRUD
@app.route('/admin/employees/add', methods=['POST'])
def add_employee():
    if 'admin' not in session: return redirect(url_for('login'))
    name = request.form.get('name')
    client_id = int(request.form.get('client_id'))
    monthly = float(request.form.get('monthly') or 0)
    rest_day = request.form.get('rest_day') or 'Sunday'
    username, pwd = gen_credentials(name)
    emp = Employee(name=name, client_id=client_id, username=username,
                   password_hash=generate_password_hash(pwd), monthly_salary=monthly,
                   rest_day=rest_day, plain_password=pwd)
    db.session.add(emp); db.session.commit()
    flash(f'Created employee {name} — username: {username} password: {pwd}', 'info')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/employees/<int:eid>/delete', methods=['POST'])
def delete_employee(eid):
    if 'admin' not in session: return redirect(url_for('login'))
    emp = Employee.query.get_or_404(eid)
    db.session.delete(emp); db.session.commit()
    return redirect(url_for('admin_dashboard'))

# timeentries (global)
@app.route('/admin/timeentries', methods=['GET','POST'])
def timeentries():
    if 'admin' not in session: return redirect(url_for('login'))
    employees = Employee.query.all()
    if request.method == 'POST':
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
    entries = TimeEntry.query.order_by(TimeEntry.date.desc()).limit(200).all()
    return render_template('timeentries.html', employees=employees, entries=entries)

# per-employee time entries & generate
@app.route('/admin/employees/<int:emp_id>/timeentries', methods=['GET','POST'])
def employee_timeentries(emp_id):
    if 'admin' not in session: return redirect(url_for('login'))
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
    if 'admin' not in session: return redirect(url_for('login'))
    emp = Employee.query.get_or_404(emp_id)
    start = datetime.strptime(request.form.get('period_start'), '%Y-%m-%d').date()
    end = datetime.strptime(request.form.get('period_end'), '%Y-%m-%d').date()
    summary = compute_payroll_for_employee(emp, start, end)
    p = Payroll(employee_id=emp.id, period_start=start, period_end=end, data=json.dumps(summary))
    db.session.add(p); db.session.commit()
    flash('Payroll generated for ' + emp.name, 'success')
    return redirect(url_for('admin_dashboard'))

# global generate (optional)
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

# ---------------------------
# Holidays admin
# ---------------------------
@app.route('/admin/holidays/add', methods=['POST'])
def add_holiday():
    if 'admin' not in session: return redirect(url_for('login'))
    date_str = request.form.get('date')
    htype = request.form.get('type')  # 'REGULAR' or 'SPECIAL'
    note = request.form.get('note')
    d = datetime.strptime(date_str, '%Y-%m-%d').date()
    if not Holiday.query.filter_by(date=d).first():
        db.session.add(Holiday(date=d, type=htype, note=note))
        db.session.commit()
    flash('Holiday added', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/holidays/<int:hid>/delete', methods=['POST'])
def delete_holiday(hid):
    if 'admin' not in session: return redirect(url_for('login'))
    h = Holiday.query.get_or_404(hid)
    db.session.delete(h); db.session.commit()
    flash('Holiday removed', 'info')
    return redirect(url_for('admin_dashboard'))

# static sample zip if you keep it
@app.route('/download/sample_zip')
def download_zip():
    return send_from_directory('/mnt/data', 'abic_payroll_app.zip', as_attachment=True)

# ---------------------------
# Create DB & seed (run at startup)
# ---------------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        # seed Mali Lending Corp. only
        if not Client.query.filter_by(name='Mali Lending Corp.').first():
            db.session.add(Client(name='Mali Lending Corp.'))
            db.session.commit()
    app.run(host="0.0.0.0", port=5000, debug=True)
