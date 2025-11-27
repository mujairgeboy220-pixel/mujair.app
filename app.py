from flask import Flask, request, redirect, session, flash, url_for, jsonify
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from config import Config
from supabase import create_client, Client
import re
import json
from datetime import datetime, timedelta
import google.generativeai as genai
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)
app.config.from_object(Config)
mail = Mail(app)

supabase: Client = create_client(
    app.config['SUPABASE_URL'],
    app.config['SUPABASE_KEY']
)

serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])

@app.before_request
def require_login_for_protected_routes():
    # daftar endpoint/public path yang boleh diakses tanpa login
    open_paths = ['/', '/login', '/register', '/forgot-password']
    # allow reset-password and static files
    if request.path.startswith('/static') or request.path.startswith('/reset-password') or request.path.startswith('/verify') or request.path.startswith('/email'):
        return None
    if request.path in open_paths or request.path.startswith('/register') or request.path.startswith('/forgot-password') or request.path.startswith('/reset-password'):
        return None

    protected_prefixes = ['/dashboard', '/kasir', '/akuntan', '/owner', '/karyawan', '/akuntan', '/kasir']
    if any(request.path.startswith(p) for p in protected_prefixes):
        if not session.get('logged_in') or 'username' not in session:
            flash('Silakan login terlebih dahulu!', 'error')
            return redirect(url_for('login'))
        # enforce role mapping for dashboard routes
        # e.g. /dashboard/kasir requires role 'kasir'
        if request.path.startswith('/dashboard/'):
            parts = request.path.split('/')
            if len(parts) > 2:
                role_needed = parts[2]
                if session.get('role') != role_needed:
                    flash('Anda tidak berhak mengakses halaman ini.', 'error')
                    return redirect(url_for('login'))
    return None

# ============== HELPER FUNCTIONS ==============
def format_rupiah(amount):
    """Format angka ke rupiah sesuai KBBI: Rp150.000"""
    if amount is None:
        return "Rp0"
    try:
        amount = float(amount)
    except:
        return "Rp0"
    if amount < 0:
        return f"-Rp{abs(amount):,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')
    
    return f"Rp{amount:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')

def parse_rupiah(rupiah_str):
    """Parse string rupiah ke float"""
    if not rupiah_str:
        return 0
    clean = rupiah_str.replace('Rp', '').replace('.', '').replace(',', '.').strip()
    try:
        return float(clean)
    except:
        return 0

def validate_password(password):
    """Validasi password sesuai ketentuan"""
    if len(password) < 8 or len(password) > 20:
        return False, "Password harus 8-20 karakter"
    if not re.search(r'[A-Z]', password):
        return False, "Password harus mengandung huruf besar"
    if not re.search(r'[a-z]', password):
        return False, "Password harus mengandung huruf kecil"
    if not re.search(r'\d', password):
        return False, "Password harus mengandung angka"
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return False, "Password harus mengandung karakter khusus (!@#$%^&*...)"
    return True, "Password valid"

def send_email(to, subject, html_content):
    """Kirim email"""
    msg = Message(subject, recipients=[to], html=html_content, sender=app.config['MAIL_DEFAULT_SENDER'])
    mail.send(msg)

def generate_transaction_code(date):
    """Generate kode transaksi format GBtgl000"""
    date_str = date.strftime('%d%m')
    try:
        today = date.strftime('%Y-%m-%d')
        response = supabase.table('transactions').select('id').gte('date', today).lt('date', today + ' 23:59:59').execute()
        count = len(response.data) + 1 if response.data else 1
        return f"GB{date_str}{count:03d}"
    except:
        return f"GB{date_str}001"

def create_adjustment_entry(date, account_code, account_name, description, debit, credit, ref_code):
    """Buat jurnal penyesuaian"""
    try:
        data = {
            'date': date,
            'account_code': account_code,
            'account_name': account_name,
            'description': description,
            'debit': float(debit) if debit else 0,
            'credit': float(credit) if credit else 0,
            'journal_type': 'AJ',
            'ref_code': ref_code
        }
        response = supabase.table('journal_entries').insert(data).execute()
        return response.data[0] if response.data else None
    except:
        return None

def create_closing_entry(date, account_code, account_name, description, debit, credit):
    """Buat jurnal penutup"""
    try:
        data = {
            'date': date,
            'account_code': account_code,
            'account_name': account_name,
            'description': description,
            'debit': float(debit) if debit else 0,
            'credit': float(credit) if credit else 0,
            'journal_type': 'CJ',
            'ref_code': 'CLOSING'
        }
        response = supabase.table('journal_entries').insert(data).execute()
        return response.data[0] if response.data else None
    except:
        return None

def create_reversing_entry(date, account_code, account_name, description, debit, credit):
    """Buat jurnal pembalik"""
    try:
        data = {
            'date': date,
            'account_code': account_code,
            'account_name': account_name,
            'description': description,
            'debit': float(debit) if debit else 0,
            'credit': float(credit) if credit else 0,
            'journal_type': 'RJ',  # Reversing Journal
            'ref_code': 'REVERSE'
        }
        response = supabase.table('journal_entries').insert(data).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error create_reversing_entry: {e}")
        return None

# ============== ASSET FUNCTIONS ==============

def create_asset(asset_name, asset_code, cost, salvage_value, useful_life, depreciation_method, purchase_date):
    """Tambah aset baru"""
    try:
        data = {
            'asset_name': asset_name,
            'asset_code': asset_code,
            'cost': float(cost),
            'salvage_value': float(salvage_value),
            'useful_life': int(useful_life),
            'depreciation_method': depreciation_method,
            'purchase_date': purchase_date,
            'accumulated_depreciation': 0,
            'book_value': float(cost)
        }
        response = supabase.table('assets').insert(data).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error create_asset: {e}")
        return None

def create_recap_posting(journal_type, period_month):
    """Posting rekapitulasi jurnal khusus ke buku besar"""
    try:
        # Ambil jurnal bulan tersebut
        start_date = f"{period_month}-01"
        end_date = f"{period_month}-31"
        
        journals = get_journal_entries(journal_type=journal_type, start_date=start_date, end_date=end_date)
        # Kelompokkan per akun
        recap = {}
        for j in journals:
            code = j['account_code']
            if code not in recap:
                recap[code] = {'name': j['account_name'], 'debit': 0, 'credit': 0}
            recap[code]['debit'] += float(j.get('debit', 0))
            recap[code]['credit'] += float(j.get('credit', 0))
        # Post rekapitulasi ke buku besar
        ref_code = f"RECAP-{journal_type}-{period_month}"
        for code, data in recap.items():
            if data['debit'] > 0 or data['credit'] > 0:
                create_journal_entry(
                    date=f"{period_month}-{datetime.now().day:02d}",
                    account_code=code,
                    account_name=data['name'],
                    description=f"Rekapitulasi {journal_type} {period_month}",
                    debit=data['debit'],
                    credit=data['credit'],
                    journal_type='GJ',  # Post ke jurnal umum
                    ref_code=ref_code
                )

        return True
    except Exception as e:
        print(f"Error create_recap_posting: {e}")
        return False

def get_all_assets():
    """Ambil semua aset"""
    try:
        response = supabase.table('assets').select('*').order('purchase_date', desc=True).execute()
        return response.data if response.data else []
    except:
        return []

def calculate_depreciation(asset, period, period_type='annual'):
    """
    Calculate depreciation based on method and period type
    Args:
        asset: Asset dictionary
        period: Period number (year or month depending on period_type)
        period_type: 'annual' or 'monthly'
    Returns:
        Depreciation amount for the specified period
    """
    cost = float(asset['cost'])
    salvage = float(asset.get('salvage_value', 0))
    useful_life = int(asset['useful_life'])
    method = asset['depreciation_method']
    if method == 'straight_line':
        # Straight Line Method
        annual_depreciation = (cost - salvage) / useful_life
        
        if period_type == 'monthly':
            return annual_depreciation / 12
        else:
            return annual_depreciation
    elif method == 'declining_balance':
        # Declining Balance Method (Double Declining)
        rate = 2 / useful_life
        book_value = cost
        if period_type == 'monthly':
            monthly_rate = rate / 12
            for i in range(1, period + 1):
                depreciation = book_value * monthly_rate
                if book_value - depreciation < salvage:
                    depreciation = max(0, book_value - salvage)
                book_value -= depreciation
                if i == period:
                    return max(0, depreciation)
        else:
            for i in range(1, period + 1):
                depreciation = book_value * rate
                if book_value - depreciation < salvage:
                    depreciation = max(0, book_value - salvage)
                book_value -= depreciation
                if i == period:
                    return max(0, depreciation)
    
    elif method == 'sum_of_years':
        # Sum of Years Digits Method
        sum_of_years = (useful_life * (useful_life + 1)) / 2
        depreciable_amount = cost - salvage
        
        if period_type == 'monthly':
            year = (period - 1) // 12 + 1
            if year <= useful_life:
                remaining_life = useful_life - year + 1
                annual_depreciation = (remaining_life / sum_of_years) * depreciable_amount
                return annual_depreciation / 12
            else:
                return 0
        else:
            if period <= useful_life:
                remaining_life = useful_life - period + 1
                return (remaining_life / sum_of_years) * depreciable_amount
            else:
                return 0
    return 0

def record_depreciation_entry(asset, depreciation_amount, period_date):
    """Catat jurnal penyusutan ke JURNAL PENYESUAIAN (AJ)"""
    try:
        ref_code = f"DEP{asset['id']}-{period_date.strftime('%Y%m')}"
        date_str = period_date.strftime('%Y-%m-%d')
        
        # ✅ Posting ke Jurnal Penyesuaian (AJ), bukan Jurnal Umum
        # 1️⃣ DEBIT: Beban Penyusutan
        create_adjustment_entry(
            date=date_str,
            account_code='6-1401',
            account_name='Beban Penyusutan Peralatan',
            description=f'Penyusutan {asset["asset_name"]}',
            debit=depreciation_amount,
            credit=0,
            ref_code=ref_code
        )
        
        # 2️⃣ KREDIT: Akumulasi Penyusutan
        create_adjustment_entry(
            date=date_str,
            account_code='1-2210',
            account_name='Akumulasi Penyusutan Peralatan',
            description=f'Penyusutan {asset["asset_name"]}',
            debit=0,
            credit=depreciation_amount,
            ref_code=ref_code
        )
        
        # Update accumulated depreciation di tabel assets
        new_accumulated = float(asset.get('accumulated_depreciation', 0)) + depreciation_amount
        new_book_value = float(asset['cost']) - new_accumulated
        
        supabase.table('assets').update({
            'accumulated_depreciation': new_accumulated,
            'book_value': new_book_value,
            'updated_at': datetime.now().isoformat()
        }).eq('id', asset['id']).execute()
        
        return True
    except Exception as e:
        print(f"❌ Error record_depreciation_entry: {e}")
        import traceback
        traceback.print_exc()
        return False

def get_asset_by_id(asset_id):
    """Ambil aset berdasarkan ID"""
    try:
        response = supabase.table('assets').select('*').eq('id', asset_id).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]
        return None
    except Exception as e:
        print(f"❌ Error get_asset_by_id: {e}")
        return None
    
def get_trial_balance(date=None):
    """Generate neraca saldo"""
    try:
        accounts = get_all_accounts()
        trial_balance = []
        
        for account in accounts:
            balance = get_ledger_balance(account['account_code'], date)
            
            if balance != 0:
                if account['normal_balance'] == 'debit':
                    debit = balance if balance > 0 else 0
                    credit = abs(balance) if balance < 0 else 0
                else:
                    credit = balance if balance > 0 else 0
                    debit = abs(balance) if balance < 0 else 0
                
                trial_balance.append({
                    'account_code': account['account_code'],
                    'account_name': account['account_name'],
                    'debit': debit,
                    'credit': credit
                })
        
        return trial_balance
    except:
        return []

def generate_income_statement(start_date, end_date):
    """Generate laporan laba rugi"""
    try:
        # Pendapatan (akun 4-xxxx)
        revenue_accounts = [acc for acc in get_all_accounts() if acc['account_code'].startswith('4-')]
        total_revenue = sum(get_ledger_balance(acc['account_code'], end_date) for acc in revenue_accounts)
        
        # Beban (akun 5-xxxx dan 6-xxxx)
        expense_accounts = [acc for acc in get_all_accounts() if acc['account_code'].startswith('5-') or acc['account_code'].startswith('6-')]
        total_expenses = sum(get_ledger_balance(acc['account_code'], end_date) for acc in expense_accounts)
        
        net_income = total_revenue - total_expenses
        
        return {
            'revenue': total_revenue,
            'expenses': total_expenses,
            'net_income': net_income,
            'revenue_details': [{
                'account_code': acc['account_code'],
                'account_name': acc['account_name'],
                'amount': get_ledger_balance(acc['account_code'], end_date)
            } for acc in revenue_accounts],
            'expense_details': [{
                'account_code': acc['account_code'],
                'account_name': acc['account_name'],
                'amount': get_ledger_balance(acc['account_code'], end_date)
            } for acc in expense_accounts]
        }
    except:
        return None

def generate_balance_sheet(date):
    """Generate neraca"""
    try:
        accounts = get_all_accounts()
        # Aset (akun 1-xxxx)
        assets = [acc for acc in accounts if acc['account_code'].startswith('1-')]
        total_assets = sum(get_ledger_balance(acc['account_code'], date) for acc in assets)
        # Kewajiban (akun 2-xxxx)
        liabilities = [acc for acc in accounts if acc['account_code'].startswith('2-')]
        total_liabilities = sum(get_ledger_balance(acc['account_code'], date) for acc in liabilities)
        # Ekuitas (akun 3-xxxx)
        equity = [acc for acc in accounts if acc['account_code'].startswith('3-')]
        total_equity = sum(get_ledger_balance(acc['account_code'], date) for acc in equity)
        
        return {
            'assets': total_assets,
            'liabilities': total_liabilities,
            'equity': total_equity,
            'asset_details': [{
                'account_code': acc['account_code'],
                'account_name': acc['account_name'],
                'amount': get_ledger_balance(acc['account_code'], date)
            } for acc in assets],
            'liability_details': [{
                'account_code': acc['account_code'],
                'account_name': acc['account_name'],
                'amount': get_ledger_balance(acc['account_code'], date)
            } for acc in liabilities],
            'equity_details': [{
                'account_code': acc['account_code'],
                'account_name': acc['account_name'],
                'amount': get_ledger_balance(acc['account_code'], date)
            } for acc in equity]
        }
    except:
        return None

def generate_cash_flow_statement(start_date, end_date):
    """Generate laporan arus kas"""
    try:
        # Ambil semua jurnal yang melibatkan Kas (1-1101)
        query = supabase.table('journal_entries').select('*').eq('account_code', '1-1000')
        if start_date:
            query = query.gte('date', start_date)
        if end_date:
            query = query.lte('date', end_date)
        response = query.order('date').execute()
        cash_entries = response.data if response.data else []
        
        # Klasifikasikan berdasarkan aktivitas
        operating = {'inflow': 0, 'outflow': 0, 'details': []}
        investing = {'inflow': 0, 'outflow': 0, 'details': []}
        financing = {'inflow': 0, 'outflow': 0, 'details': []}
        
        for entry in cash_entries:
            debit = float(entry.get('debit', 0))
            credit = float(entry.get('credit', 0))
            description = entry.get('description', '')
            ref_code = entry.get('ref_code', '')
            
            # Klasifikasi berdasarkan deskripsi/ref_code
            # Operasional: penjualan, pembelian bibit, beban
            if 'penjualan' in description.lower() or ref_code.startswith('GB'):
                if debit > 0:
                    operating['inflow'] += debit
                    operating['details'].append({
                        'date': entry['date'],
                        'description': description,
                        'amount': debit,
                        'type': 'in'
                    })
            
            # Operasional: pembelian, beban
            elif 'pembelian' in description.lower() or 'beban' in description.lower() or 'gaji' in description.lower() or 'listrik' in description.lower():
                if credit > 0:
                    operating['outflow'] += credit
                    operating['details'].append({
                        'date': entry['date'],
                        'description': description,
                        'amount': credit,
                        'type': 'out'
                    })
            
            # Investasi: pembelian/penjualan peralatan
            elif 'peralatan' in description.lower() or 'aset' in description.lower():
                if credit > 0:
                    investing['outflow'] += credit
                    investing['details'].append({
                        'date': entry['date'],
                        'description': description,
                        'amount': credit,
                        'type': 'out'
                    })
                if debit > 0:
                    investing['inflow'] += debit
                    investing['details'].append({
                        'date': entry['date'],
                        'description': description,
                        'amount': debit,
                        'type': 'in'
                    })
            
            # Pendanaan: modal, prive, utang
            elif 'modal' in description.lower() or 'prive' in description.lower() or 'utang' in description.lower():
                if debit > 0:
                    financing['inflow'] += debit
                    financing['details'].append({
                        'date': entry['date'],
                        'description': description,
                        'amount': debit,
                        'type': 'in'
                    })
                if credit > 0:
                    financing['outflow'] += credit
                    financing['details'].append({
                        'date': entry['date'],
                        'description': description,
                        'amount': credit,
                        'type': 'out'
                    })
        
        # Hitung net cash flow
        net_operating = operating['inflow'] - operating['outflow']
        net_investing = investing['inflow'] - investing['outflow']
        net_financing = financing['inflow'] - financing['outflow']
        net_change = net_operating + net_investing + net_financing
        
        # Kas awal
        accounts = get_all_accounts()
        beginning_cash_account = next((a for a in accounts if a['account_code'] == '1-1000'), None)
        beginning_cash = float(beginning_cash_account.get('beginning_balance', 0)) if beginning_cash_account else 0
        
        ending_cash = beginning_cash + net_change
        
        return {
            'operating': operating,
            'investing': investing,
            'financing': financing,
            'net_operating': net_operating,
            'net_investing': net_investing,
            'net_financing': net_financing,
            'net_change': net_change,
            'beginning_cash': beginning_cash,
            'ending_cash': ending_cash
        }
    except Exception as e:
        print(f"❌ Error generate_cash_flow_statement: {e}")
        import traceback
        traceback.print_exc()
        return None  # ✅ PASTIKAN RETURN None JIKA ERROR

# ============== DATABASE FUNCTIONS ==============
def get_user_by_email(email):
    """Ambil user dari database berdasarkan email"""
    try:
        response = supabase.table('users').select('*').eq('email', email).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]
        return None
    except Exception as e:
        print(f"Error get_user_by_email: {e}")
        return None
    
def get_user_by_username(username):
    """Ambil user dari database berdasarkan username"""
    try:
        response = supabase.table('users').select('*').eq('username', username).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]
        return None
    except Exception as e:
        print(f"Error get_user_by_username: {e}")
        return None

def create_user(email, username, password, role):
    """Buat user baru di database"""
    try:
        password_hash = generate_password_hash(password)
        data = {
            'email': email,
            'username': username,
            'password_hash': password_hash,
            'role': role
        }
        response = supabase.table('users').insert(data).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error create_user: {e}")
        return None
    
def create_pending_registration(email, role, token):
    """Buat pending registration di database"""
    try:
        # Hapus pending registration lama dengan email yang sama
        try:
            supabase.table('pending_registrations').delete().eq('email', email).execute()
        except:
            pass

        expires_at = (datetime.now() + timedelta(hours=1)).isoformat()
        data = {
            'email': email,
            'role': role,  # <-- Pastikan role terkirim dengan benar
            'token': token,
            'expires_at': expires_at
        }
        
        print(f"🔍 SAVING PENDING REG: email={email}, role={role}")  # Debug
        
        response = supabase.table('pending_registrations').insert(data).execute()
        
        print(f"✅ Response: {response.data}")  # Debug
        
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"❌ Error: {e}")
        return None
def get_pending_registration(email):
    """Ambil pending registration berdasarkan email"""
    try:
        response = supabase.table('pending_registrations').select('*').eq('email', email).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]
        return None
    except Exception as e:
        print(f"Error get_pending_registration: {e}")
        return None

def delete_pending_registration(email):
    """Hapus pending registration setelah berhasil verifikasi"""
    try:
        supabase.table('pending_registrations').delete().eq('email', email).execute()
        return True
    except Exception as e:
        print(f"Error delete_pending_registration: {e}")
        return False

def update_user_password(email, new_password):
    """Update password user"""
    try:
        password_hash = generate_password_hash(new_password)
        data = {'password_hash': password_hash, 'updated_at': datetime.now().isoformat()}
        response = supabase.table('users').update(data).eq('email', email).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error update_user_password: {e}")
        return None

# ============== ACCOUNTING DATABASE FUNCTIONS ==============

def get_all_accounts():
    try:
        response = supabase.table('accounts').select('*').order('account_code').execute()
        return response.data if response.data else []
    except:
        return []

def create_account(account_code, account_name, account_type, normal_balance, beginning_balance=0):
    """Buat akun baru di database"""
    try:
        # ✅ VALIDASI DATA
        if not account_code or not account_name:
            print("❌ Account code/name is empty")
            return None
        
        # ✅ CEK DUPLIKASI
        existing = supabase.table('accounts').select('account_code').eq('account_code', account_code).execute()
        if existing.data:
            print(f"❌ Account {account_code} already exists")
            return None
        
        # ✅ PASTIKAN BEGINNING_BALANCE ADALAH FLOAT
        try:
            beginning_balance = float(beginning_balance) if beginning_balance else 0
        except:
            beginning_balance = 0
        
        data = {
            'account_code': account_code,
            'account_name': account_name,
            'account_type': account_type,
            'normal_balance': normal_balance,
            'beginning_balance': beginning_balance
        }
        
        response = supabase.table('accounts').insert(data).execute()
        
        if response.data:
            print(f"✅ Account created: {account_code} - {account_name}")
            return response.data[0]
        else:
            print(f"❌ No data returned from insert")
            return None
            
    except Exception as e:
        print(f"❌ Error create_account: {e}")
        import traceback
        traceback.print_exc()
        return None
    
def create_journal_entry(date, account_code, account_name, description, debit, credit, journal_type, ref_code):
    try:
        data = {
            'date': date,
            'account_code': account_code,
            'account_name': account_name,
            'description': description,
            'debit': float(debit) if debit else 0,
            'credit': float(credit) if credit else 0,
            'journal_type': journal_type,
            'ref_code': ref_code
        }
        response = supabase.table('journal_entries').insert(data).execute()
        return response.data[0] if response.data else None
    except:
        return None
    
def get_journal_entries(journal_type=None, start_date=None, end_date=None):
    try:
        query = supabase.table('journal_entries').select('*')
        if journal_type:
            query = query.eq('journal_type', journal_type)
        if start_date:
            query = query.gte('date', start_date)
        if end_date:
            query = query.lte('date', end_date)
        response = query.order('date').execute()
        return response.data if response.data else []
    except:
        return []
    
def create_transaction(transaction_code, items, total_amount, cashier_username):
    """Kasir input penjualan - METODE PERPETUAL (4 AKUN) - FIXED"""
    try:
        data = {
            'transaction_code': transaction_code,
            'date': datetime.now().isoformat(),
            'items': json.dumps(items),
            'total_amount': float(total_amount),
            'payment_method': 'cash',
            'cashier_username': cashier_username
        }
        response = supabase.table('transactions').insert(data).execute()
        
        if response.data:
            date_str = datetime.now().strftime('%Y-%m-%d')
            
            # ===== METODE PERPETUAL - 4 AKUN =====
            
            # 1️⃣ DEBIT: KAS
            create_journal_entry(
                date=date_str,
                account_code='1-1000',
                account_name='Kas',
                description=f'Penjualan tunai {transaction_code}',
                debit=total_amount,      # ✅ FIXED
                credit=0,                 # ✅ FIXED
                journal_type='GJ',
                ref_code=transaction_code
            )
            
            # 2️⃣ KREDIT: PENJUALAN
            create_journal_entry(
                date=date_str,
                account_code='4-1000',
                account_name='Penjualan',
                description=f'Penjualan tunai {transaction_code}',
                debit=0,                  # ✅ FIXED
                credit=total_amount,      # ✅ FIXED
                journal_type='GJ',
                ref_code=transaction_code
            )
            
            # ===== HITUNG HPP DARI INVENTORY CARD =====
            total_hpp = 0
            
            for item in items:
                # Ambil HPP terakhir dari inventory card
                last_entry = supabase.table('inventory_card')\
                    .select('*')\
                    .eq('product_name', item['name'])\
                    .order('id', desc=True)\
                    .limit(1)\
                    .execute()
                
                if last_entry.data and last_entry.data[0].get('balance_unit_price'):
                    hpp_per_unit = float(last_entry.data[0]['balance_unit_price'])
                else:
                    # Jika belum ada di inventory, estimasi HPP = 70% harga jual
                    hpp_per_unit = item['price'] * 0.7
                
                item_hpp = item['quantity'] * hpp_per_unit
                total_hpp += item_hpp
                
                # ✅ KURANGI DARI INVENTORY CARD
                last_qty = last_entry.data[0]['balance_quantity'] if last_entry.data else 0
                last_balance_amount = last_entry.data[0]['balance_amount'] if last_entry.data else 0
                
                new_balance_qty = last_qty - item['quantity']
                new_balance_amount = last_balance_amount - item_hpp
                
                supabase.table('inventory_card').insert({
                    'date': date_str,
                    'doc_no': transaction_code,
                    'description': f'Penjualan kepada pelanggan',
                    'product_name': item['name'],
                    'purchase_quantity': 0,
                    'purchase_unit_price': 0,
                    'purchase_amount': 0,
                    'sales_quantity': item['quantity'],
                    'sales_unit_price': hpp_per_unit,
                    'sales_amount': item_hpp,
                    'balance_quantity': new_balance_qty,
                    'balance_unit_price': hpp_per_unit,
                    'balance_amount': new_balance_amount,
                    'employee': cashier_username
                }).execute()
            
            # 3️⃣ DEBIT: HPP
            create_journal_entry(
                date=date_str,
                account_code='5-1000',
                account_name='Harga Pokok Penjualan',
                description=f'HPP penjualan {transaction_code}',
                debit=total_hpp,          # ✅ FIXED
                credit=0,                  # ✅ FIXED
                journal_type='GJ',
                ref_code=transaction_code
            )
            
            # 4️⃣ KREDIT: PERSEDIAAN
            create_journal_entry(
                date=date_str,
                account_code='1-1200',
                account_name='Persediaan Ikan Mujair',
                description=f'HPP penjualan {transaction_code}',
                debit=0,                   # ✅ FIXED
                credit=total_hpp,          # ✅ FIXED
                journal_type='GJ',
                ref_code=transaction_code
            )

        return response.data[0] if response.data else None
    except Exception as e:
        print(f"❌ Error create_transaction: {e}")
        import traceback
        traceback.print_exc()
        return None
                
def get_transactions(start_date=None, end_date=None):
    try:
        query = supabase.table('transactions').select('*')
        if start_date:
            query = query.gte('date', start_date)
        if end_date:
            query = query.lte('date', end_date + ' 23:59:59')
        response = query.order('date', desc=True).execute()
        return response.data if response.data else []
    except:
        return []

def process_sale_transaction(date, customer, quantity, unit_price, sale_price, description, cashier):
    """
    Process penjualan lengkap:
    1. Kurangi inventory (quantity_out)
    2. Hitung HPP
    3. Create jurnal penjualan
    4. Create jurnal HPP
    """
    try:
        # 1. Insert ke tabel sales
        sale = supabase.table('sales').insert({
            'date': date,
            'customer': customer,
            'quantity': quantity,
            'unit_price': sale_price,
            'total_amount': quantity * sale_price,
            'description': description,
            'cashier': cashier,
            'status': 'completed'
        }).execute()
        
        if not sale.data:
            return {'success': False, 'message': 'Gagal insert penjualan'}
        
        sale_id = sale.data[0]['id']
        ref_code = f"SL{sale_id:04d}"
        
        # 2. Kurangi inventory (barang keluar)
        inventory_entry = create_inventory_entry(
            date=date,
            ref_code=ref_code,
            description=f"Penjualan - {description}",
            quantity_in=0,
            quantity_out=quantity,
            unit_price=unit_price,  # HPP (harga beli)
            employee=cashier
        )
        
        if not inventory_entry:
            # Rollback penjualan
            supabase.table('sales').delete().eq('id', sale_id).execute()
            return {'success': False, 'message': 'Gagal update inventory'}
        
        # 3. Create jurnal penjualan
        # Dr. Kas / Piutang    xxx
        #     Cr. Penjualan        xxx
        sales_amount = quantity * sale_price
        create_journal_entry(
            date=date,
            ref_code=ref_code,
            description=f"Penjualan {quantity} kg Ikan Mujair",
            debit_account='Kas',  # atau 'Piutang Usaha' jika kredit
            credit_account='Penjualan',
            amount=sales_amount
        )
        
        # 4. Create jurnal HPP (Harga Pokok Penjualan)
        # Dr. HPP               xxx
        #     Cr. Persediaan       xxx
        hpp_amount = quantity * unit_price
        create_journal_entry(
            date=date,
            ref_code=ref_code,
            description=f"HPP - Penjualan {quantity} kg Ikan Mujair",
            debit_account='Harga Pokok Penjualan',
            credit_account='Persediaan Barang Dagang',
            amount=hpp_amount
        )
        
        return {
            'success': True,
            'message': 'Penjualan berhasil diproses!',
            'sale_id': sale_id,
            'inventory_id': inventory_entry['id'],
            'sales_amount': sales_amount,
            'hpp_amount': hpp_amount,
            'profit': sales_amount - hpp_amount
        }
        
    except Exception as e:
        print(f"❌ Error process_sale: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'message': str(e)}

def create_purchase(item_type, item_name, quantity, unit_price, total_amount, employee_username, receipt_image=''):
    """Karyawan input pembelian - METODE PERPETUAL"""
    try:
        data = {
            'date': datetime.now().isoformat(),
            'item_type': item_type,
            'item_name': item_name,
            'quantity': float(quantity),
            'unit_price': float(unit_price),
            'total_amount': float(total_amount),
            'receipt_image': receipt_image,
            'employee_username': employee_username,
            'status': 'approved'
        }
        
        response = supabase.table('purchases').insert(data).execute()
        
        if response.data:
            purchase = response.data[0]
            date_str = datetime.now().strftime('%Y-%m-%d')
            ref_code = f"BL{datetime.now().strftime('%d%m')}{purchase['id']:03d}"
            
            # ✅ MAPPING AKUN METODE PERPETUAL
            account_mapping = {
                'bibit': {
                    'debit': ('1-1200', 'Persediaan Ikan Mujair'),
                    'credit': ('1-1000', 'Kas')
                },
                'perlengkapan': {
                    'debit': ('1-1300', 'Perlengkapan'),
                    'credit': ('1-1000', 'Kas')
                },
                'peralatan': {
                    'debit': ('1-2200', 'Peralatan'),
                    'credit': ('1-1000', 'Kas')
                }
            }
            
            mapping = account_mapping.get(item_type)
            if not mapping:
                return None
            
            # 1️⃣ DEBIT: Persediaan/Peralatan/Perlengkapan
            create_journal_entry(
                date=date_str,
                account_code=mapping['debit'][0],
                account_name=mapping['debit'][1],
                description=f'Pembelian {item_name}',
                debit_account=total_amount,
                credit_account=0,
                journal_type='GJ',
                ref_code=ref_code
            )
            
            # 2️⃣ KREDIT: Kas
            create_journal_entry(
                date=date_str,
                account_code=mapping['credit'][0],
                account_name=mapping['credit'][1],
                description=f'Pembelian {item_name}',
                debit_account=0,
                credit_account=total_amount,
                journal_type='GJ',
                ref_code=ref_code
            )
            
            # ✅ OTOMATIS TAMBAHKAN KE INVENTORY CARD (jika bibit)
            if item_type == 'bibit':
                last_entry = supabase.table('inventory_card')\
                    .select('*')\
                    .eq('product_name', item_name)\
                    .order('id', desc=True)\
                    .limit(1)\
                    .execute()
                
                last_qty = last_entry.data[0]['balance_quantity'] if last_entry.data else 0
                new_balance = last_qty + quantity
                
                supabase.table('inventory_card').insert({
                    'date': date_str,
                    'doc_no': ref_code,
                    'description': f'Pembelian bibit',
                    'product_name': item_name,
                    'purchase_quantity': quantity,
                    'purchase_unit_price': unit_price,
                    'purchase_amount': total_amount,
                    'sales_quantity': 0,
                    'sales_unit_price': 0,
                    'sales_amount': 0,
                    'balance_quantity': new_balance,
                    'balance_unit_price': unit_price,
                    'balance_amount': new_balance * unit_price,
                    'employee': employee_username
                }).execute()
            
            return response.data[0]
        return None
            
    except Exception as e:
        print(f"❌ Error create_purchase: {e}")
        import traceback
        traceback.print_exc()
        return None

def get_purchases():
    try:
        response = supabase.table('purchases').select('*').order('date', desc=True).execute()
        return response.data if response.data else []
    except:
        return []

def get_ledger_balance(account_code, end_date=None):
    """Hitung saldo buku besar"""
    try:
        accounts = get_all_accounts()
        account = next((acc for acc in accounts if acc['account_code'] == account_code), None)
        
        if not account:
            return 0
        
        # Ambil semua journal entries untuk akun ini
        query = supabase.table('journal_entries').select('*').eq('account_code', account_code)
        if end_date:
            query = query.lte('date', end_date)
        response = query.execute()
        entries = response.data if response.data else []
        
        # Hitung saldo dari beginning balance
        balance = float(account.get('beginning_balance', 0))
        
        # Tambahkan/kurangi dari journal entries
        for entry in entries:
            if account['normal_balance'] == 'debit':
                balance += float(entry.get('debit', 0)) - float(entry.get('credit', 0))
            else:
                balance += float(entry.get('credit', 0)) - float(entry.get('debit', 0))
        
        return balance
    except:
        return 0

# ============== INVENTORY CARD FUNCTIONS ==============

# GANTI fungsi-fungsi ini di app.py
def create_inventory_card(
    date,
    product_name,
    quantity_in,
    quantity_out,
    unit_price,
    total_hpp,
    ref_code,
    description="",
    employee=""
):
    """Insert transaksi masuk/keluar ke inventory card + update saldo"""
    try:
        # Tentukan balance qty (butuh entry terakhir)
        last = supabase.table("inventory_card") \
            .select("*") \
            .eq("product_name", product_name) \
            .order("id", desc=True) \
            .limit(1) \
            .execute()

        last_qty = last.data[0]["balance_quantity"] if last.data else 0
        balance_qty = last_qty + float(quantity_in) - float(quantity_out)

        # Insert row baru
        supabase.table("inventory_card").insert({
            "date": date,
            "product_name": product_name,
            "quantity_in": quantity_in,
            "quantity_out": quantity_out,
            "unit_price": unit_price,
            "total_hpp": total_hpp,
            "balance_quantity": balance_qty,
            "ref_code": ref_code,
            "description": description,
            "employee": employee
        }).execute()

        return True

    except Exception as e:
        print("ERROR create_inventory_card:", e)
        return False

def create_inventory_entry(date, ref_code, description, quantity_in=0, quantity_out=0, unit_price=0, employee=""):
    """
    Fungsi universal untuk create inventory entry
    Otomatis hitung balance
    """
    try:
        # Ambil balance terakhir
        last_entry = supabase.table('inventory_card')\
            .select('balance_quantity')\
            .order('id', desc=True)\
            .limit(1)\
            .execute()
        
        last_balance = last_entry.data[0]['balance_quantity'] if last_entry.data else 0
        new_balance = last_balance + quantity_in - quantity_out
        
        # Hitung HPP (untuk transaksi keluar)
        total_hpp = quantity_out * unit_price if quantity_out > 0 else 0
        
        # Insert ke inventory_card
        result = supabase.table('inventory_card').insert({
            'date': date,
            'product_name': 'Ikan Mujair',  # Hardcode karena cuma 1 produk
            'ref_code': ref_code,
            'description': description,
            'quantity_in': quantity_in,
            'quantity_out': quantity_out,
            'balance_quantity': new_balance,
            'unit_price': unit_price,
            'total_hpp': total_hpp,
            'employee': employee
        }).execute()
        
        print(f"✅ Inventory entry created: {ref_code}")
        return result.data[0] if result.data else None
        
    except Exception as e:
        print(f"❌ Error create_inventory_entry: {e}")
        import traceback
        traceback.print_exc()
        return None

@app.route('/akuntan/inventory-card/add', methods=['POST'])
def akuntan_inventory_add():
    """Tambah entry manual inventory card (sudah ada di kode sebelumnya)"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        data = request.get_json()
        date = data.get("date")
        ref_code = data.get("ref_code", "MANUAL")
        description = data.get("description", "")
        quantity_in = float(data.get("quantity_in", 0))
        quantity_out = float(data.get("quantity_out", 0))
        unit_price = float(data.get("unit_price", 0))
        username = session.get("username")

        # Validasi
        if not date:
            return jsonify({"success": False, "message": "Tanggal wajib diisi!"}), 400

        # Create inventory entry
        entry = create_inventory_entry(
            date=date,
            ref_code=ref_code,
            description=description,
            quantity_in=quantity_in,
            quantity_out=quantity_out,
            unit_price=unit_price,
            employee=username
        )

        if entry:
            return jsonify({"success": True, "message": "Entry berhasil ditambahkan"}), 200
        else:
            return jsonify({"success": False, "message": "Insert gagal!"}), 500

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": str(e)}), 500

def get_all_inventory_card(product_name=None):
    """Ambil semua inventory card"""
    try:
        query = supabase.table('inventory_card').select('*')
        if product_name:
            query = query.eq('product_name', product_name)
        response = query.order('date').execute()
        return response.data if response.data else []
    except:
        return []

def get_last_inventory_entry(product_name):
    """Get last inventory entry for a product - FIXED"""
    try:
        response = supabase.table("inventory_card")\
            .select("*")\
            .eq("product_name", product_name)\
            .order("id", desc=True)\
            .limit(1)\
            .execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error get_last_inventory_entry: {e}")
        return None

def update_inventory_card(card_id, product_name, date, ref_code, description, quantity_in, quantity_out, unit_price):
    """Update inventory card"""
    try:
        # Recalculate balance (simplified - should recalculate all subsequent entries)
        data = {
            'product_name': product_name,
            'date': date,
            'ref_code': ref_code,
            'description': description,
            'quantity_in': float(quantity_in),
            'quantity_out': float(quantity_out),
            'unit_price': float(unit_price),
            'updated_at': datetime.now().isoformat()
        }
        
        response = supabase.table('inventory_card').update(data).eq('id', card_id).execute()
        
        # Recalculate all balances after this entry
        recalculate_inventory_balances(product_name)
        
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error update_inventory_card: {e}")
        return None

def delete_inventory_card(card_id):
    """Hapus inventory card - FINAL VERSION"""
    try:
        print(f"🔍 Deleting card_id: {card_id}")
        
        # Delete entry
        result = supabase.table('inventory_card')\
            .delete()\
            .eq('id', card_id)\
            .execute()
        
        print(f"🗑️ Delete executed")
        
        # Recalculate all balances
        recalculate_inventory_balances()
        
        return True
        
    except Exception as e:
        print(f"❌ Error delete: {e}")
        import traceback
        traceback.print_exc()
        return False

def recalculate_inventory_balances():
    """Recalculate semua balance inventory secara kronologis"""
    try:
        # Ambil semua entry, urutkan by date & id
        entries = supabase.table('inventory_card')\
            .select('*')\
            .order('date')\
            .order('id')\
            .execute()
        
        if not entries.data:
            return True
        
        balance_qty = 0
        
        for entry in entries.data:
            # Hitung balance kumulatif
            balance_qty += entry.get('quantity_in', 0) - entry.get('quantity_out', 0)
            
            # Update balance
            supabase.table('inventory_card').update({
                'balance_quantity': balance_qty
            }).eq('id', entry['id']).execute()
        
        print(f"✅ Recalculated {len(entries.data)} entries")
        return True
        
    except Exception as e:
        print(f"❌ Error recalculate: {e}")
        return False
    
def get_inventory_summary():
    """Get summary of all products in inventory"""
    try:
        # Get all products
        response = supabase.table('inventory_card').select('product_name').execute()
        products = list(set([item['product_name'] for item in response.data])) if response.data else []
        
        summary = []
        for product in products:
            last_entry = get_last_inventory_entry(product)
            if last_entry:
                summary.append({
                    'product_name': product,
                    'balance_quantity': last_entry['balance_quantity'],
                    'unit_price': last_entry['unit_price'],
                    'total_value': last_entry['balance_quantity'] * last_entry['unit_price']
                })
        
        return summary
    except:
        return []

# ============== STYLE GENERATORS ==============
def generate_base_style():
    """Generate CSS base style"""
    return """
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            padding: 40px;
            max-width: 500px;
            width: 100%;
        }
        .logo { font-size: 50px; text-align: center; margin-bottom: 10px; }
        h1 { color: #667eea; text-align: center; margin-bottom: 30px; font-size: 28px; }
        .subtitle { text-align: center; color: #666; margin-bottom: 30px; font-size: 14px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; color: #333; font-weight: bold; margin-bottom: 8px; }
        input, select {
            width: 100%;
            padding: 12px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-size: 14px;
            transition: border-color 0.3s;
        }
        input:focus, select:focus { outline: none; border-color: #667eea; }
        .btn {
            width: 100%;
            padding: 15px;
            border: none;
            border-radius: 10px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            transition: all 0.3s;
            background: #667eea;
            color: white;
        }
        .btn:hover {
            background: #5568d3;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }
        .links { text-align: center; margin-top: 20px; }
        .links a { color: #667eea; text-decoration: none; font-size: 14px; }
        .links a:hover { text-decoration: underline; }
        .alert {
            padding: 12px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 14px;
        }
        .alert-success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .alert-error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .password-requirements {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-size: 13px;
        }
        .password-requirements h3 {
            color: #333;
            font-size: 14px;
            margin-bottom: 10px;
        }
        .password-requirements ul {
            margin-left: 20px;
            color: #666;
        }
        .password-requirements li { margin-bottom: 5px; }
    </style>
    """

def generate_dashboard_style():
    return """
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: #f5f6fa;
            min-height: 100vh;
        }
        .dashboard-container {
            display: flex;
            min-height: 100vh;
        }
        .sidebar {
            width: 280px;
            background: linear-gradient(180deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px;
            box-shadow: 2px 0 10px rgba(0,0,0,0.1);
            position: fixed;
            height: 100vh;
            overflow-y: auto;
        }
        .sidebar-header {
            text-align: center;
            padding: 20px 0;
            border-bottom: 2px solid rgba(255,255,255,0.2);
            margin-bottom: 20px;
        }
        .sidebar-logo { font-size: 50px; margin-bottom: 10px; }
        .sidebar-title { font-size: 24px; font-weight: bold; margin-bottom: 5px; }
        .sidebar-subtitle { font-size: 12px; opacity: 0.9; }
        .sidebar-user {
            background: rgba(255,255,255,0.1);
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 20px;
            text-align: center;
        }
        .sidebar-user-icon { font-size: 40px; margin-bottom: 10px; }
        .sidebar-user-name { font-weight: bold; margin-bottom: 5px; }
        .sidebar-user-role { font-size: 12px; opacity: 0.8; text-transform: capitalize; }
        .sidebar-menu { list-style: none; }
        .sidebar-menu li { margin-bottom: 5px; }
        .sidebar-menu a {
            display: flex;
            align-items: center;
            gap: 15px;
            padding: 15px;
            color: white;
            text-decoration: none;
            border-radius: 10px;
            transition: all 0.3s;
        }
        .sidebar-menu a:hover, .sidebar-menu a.active {
            background: rgba(255,255,255,0.2);
            transform: translateX(5px);
        }
        .sidebar-menu .icon {
            font-size: 24px;
            width: 30px;
            text-align: center;
        }
        .main-content {
            margin-left: 280px;
            padding: 30px;
            width: calc(100% - 280px);
        }
        .top-bar {
            background: white;
            padding: 20px 30px;
            border-radius: 15px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            margin-bottom: 30px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .top-bar h1 {
            color: #333;
            font-size: 28px;
        }
        .top-bar .date-time {
            color: #666;
            font-size: 14px;
        }
        .content-section {
            background: white;
            padding: 30px;
            border-radius: 15px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }
        .content-section h2 {
            color: #667eea;
            margin-bottom: 20px;
            font-size: 24px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 25px;
            border-radius: 15px;
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.3);
        }
        .stat-icon {
            font-size: 40px;
            margin-bottom: 15px;
        }
        .stat-value {
            font-size: 32px;
            font-weight: bold;
            margin-bottom: 5px;
        }
        .stat-label {
            font-size: 14px;
            opacity: 0.9;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        th {
            background: #667eea;
            color: white;
            padding: 15px;
            text-align: left;
            font-weight: bold;
        }
        th.text-right, td.text-right {
            text-align: right;
        }
        th.text-center, td.text-center {
            text-align: center;
        }
        td {
            padding: 12px 15px;
            border-bottom: 1px solid #e0e0e0;
        }
        tr:hover {
            background: #f8f9fa;
        }
        .btn-group {
            display: flex;
            gap: 10px;
            justify-content: center;
        }
        .btn-sm {
            padding: 8px 16px;
            font-size: 14px;
            border-radius: 6px;
            border: none;
            cursor: pointer;
            transition: all 0.3s;
            text-decoration: none;
            display: inline-block;
            color: white;
        }
        .btn-primary { background: #667eea; }
        .btn-primary:hover { background: #5568d3; }
        .btn-warning { background: #ffc107; color: #333; }
        .btn-warning:hover { background: #e0a800; }
        .btn-danger { background: #dc3545; }
        .btn-danger:hover { background: #c82333; }
        .btn-success { background: #28a745; }
        .btn-success:hover { background: #218838; }
        .btn-info { background: #17a2b8; }
        .btn-info:hover { background: #138496; }
        .form-row {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .form-group {
            margin-bottom: 15px;
        }
        .form-group label {
            display: block;
            color: #333;
            font-weight: bold;
            margin-bottom: 8px;
        }
        .form-group input,
        .form-group select,
        .form-group textarea {
            width: 100%;
            padding: 10px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-size: 14px;
        }
        .cart-items {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            max-height: 400px;
            overflow-y: auto;
        }
        .cart-item {
            background: white;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .cart-total {
            background: #667eea;
            color: white;
            padding: 20px;
            border-radius: 10px;
            margin-top: 20px;
            text-align: right;
        }
        .cart-total h3 {
            font-size: 32px;
            margin-top: 10px;
        }
        .receipt {
            background: white;
            padding: 40px;
            max-width: 400px;
            margin: 0 auto;
            border: 2px dashed #333;
            font-family: 'Courier New', monospace;
            font-size: 14px;
        }
        .receipt-header {
            text-align: center;
            border-bottom: 2px dashed #333;
            padding-bottom: 20px;
            margin-bottom: 20px;
        }
        .receipt-title {
            font-size: 24px;
            font-weight: bold;
            margin-bottom: 10px;
        }
        .receipt-address {
            font-size: 12px;
            line-height: 1.6;
        }
        .receipt-info {
            margin-bottom: 20px;
            font-size: 12px;
        }
        .receipt-items {
            margin-bottom: 20px;
        }
        .receipt-item {
            display: flex;
            justify-content: space-between;
            margin-bottom: 8px;
            font-size: 13px;
        }
        .receipt-line {
            border-top: 2px dashed #333;
            margin: 20px 0;
        }
        .receipt-total {
            font-size: 18px;
            font-weight: bold;
            display: flex;
            justify-content: space-between;
            margin-top: 10px;
        }
        .receipt-footer {
            border-top: 2px dashed #333;
            padding-top: 20px;
            margin-top: 20px;
            text-align: center;
            font-size: 12px;
        }
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
        }
        .modal-content {
            background: white;
            margin: 50px auto;
            padding: 30px;
            border-radius: 15px;
            max-width: 800px;
            max-height: 80vh;
            overflow-y: auto;
        }
        .close {
            float: right;
            font-size: 28px;
            font-weight: bold;
            cursor: pointer;
            color: #999;
        }
        .close:hover {
            color: #333;
        }
        .btn-block {
            width: 100%;
            padding: 15px;
            margin-bottom: 10px;
        }
        @media print {
            .sidebar, .top-bar, .btn, .no-print {
                display: none !important;
            }
            .main-content {
                margin-left: 0;
                width: 100%;
            }
        }
    </style>
    <script>
        function updateDateTime() {
            const now = new Date();
            const options = { 
                weekday: 'long', 
                year: 'numeric', 
                month: 'long', 
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            };
            const dateTimeStr = now.toLocaleDateString('id-ID', options);
            const elem = document.getElementById('datetime');
            if (elem) elem.textContent = dateTimeStr;
        }
        setInterval(updateDateTime, 1000);
        window.onload = updateDateTime;
    </script>
    """

# ============== PAGE GENERATORS ==============
def generate_index_page():
    """Generate halaman index (home)"""
    style = """
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            padding: 50px;
            max-width: 500px;
            width: 100%;
            text-align: center;
        }
        .logo { font-size: 60px; margin-bottom: 10px; }
        h1 { color: #667eea; margin-bottom: 10px; font-size: 36px; }
        .subtitle { color: #666; margin-bottom: 40px; font-size: 14px; }
        .role-selection { margin-bottom: 30px; }
        .role-selection h2 {
            color: #333;
            margin-bottom: 20px;
            font-size: 20px;
        }
        .role-buttons {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }
        .role-btn {
            background: white;
            border: 2px solid #667eea;
            color: #667eea;
            padding: 20px;
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.3s;
            font-size: 16px;
            font-weight: bold;
            text-decoration: none;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 10px;
        }
        .role-btn:hover {
            background: #667eea;
            color: white;
            transform: translateY(-5px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.3);
        }
        .role-btn .icon { font-size: 30px; }
        .auth-buttons {
            display: flex;
            gap: 15px;
            margin-top: 30px;
        }
        .btn {
            flex: 1;
            padding: 15px;
            border: none;
            border-radius: 10px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            transition: all 0.3s;
            text-decoration: none;
            display: inline-block;
        }
        .btn-primary {
            background: #667eea;
            color: white;
        }
        .btn-primary:hover {
            background: #5568d3;
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }
        .btn-secondary {
            background: #f0f0f0;
            color: #333;
        }
        .btn-secondary:hover {
            background: #e0e0e0;
            transform: translateY(-2px);
        }
    </style>
    """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Geboy Mujair - Sistem Akuntansi Budidaya Ikan</title>
        {style}
    </head>
    <body>
        <div class="container">
            <div class="logo">🐟</div>
            <h1>Geboy Mujair</h1>
            <p class="subtitle">Sistem Akuntansi Budidaya Ikan Mujair</p>
            
            <div class="role-selection">
                <h2>Pilih Role Anda</h2>
                <div class="role-buttons">
                    <a href="/register?role=kasir" class="role-btn">
                        <span class="icon">💰</span>
                        <span>Kasir</span>
                    </a>
                    <a href="/register?role=akuntan" class="role-btn">
                        <span class="icon">📊</span>
                        <span>Akuntan</span>
                    </a>
                    <a href="/register?role=owner" class="role-btn">
                        <span class="icon">👔</span>
                        <span>Owner</span>
                    </a>
                    <a href="/register?role=karyawan" class="role-btn">
                        <span class="icon">👷</span>
                        <span>Karyawan</span>
                    </a>
                </div>
            </div>
            
            <div class="auth-buttons">
                <a href="/login" class="btn btn-primary">Login</a>
                <a href="/register" class="btn btn-secondary">Daftar</a>
            </div>
        </div>
    </body>
    </html>
    """
    return html

def generate_register_page(role=''):
    """Generate halaman registrasi"""
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daftar - Geboy Mujair</title>
        {generate_base_style()}
    </head>
    <body>
        <div class="container">
            <div class="logo">🐟</div>
            <h1>Daftar Akun</h1>
            {flash_html}
            <form method="POST" action="/register">
                <div class="form-group">
                    <label for="email">Email</label>
                    <input type="email" id="email" name="email" required placeholder="email@example.com">
                </div>
                <div class="form-group">
                    <label for="role">Role</label>
                    <select id="role" name="role" required>
                        <option value="">-- Pilih Role --</option>
                        <option value="kasir" {'selected' if role == 'kasir' else ''}>Kasir</option>
                        <option value="akuntan" {'selected' if role == 'akuntan' else ''}>Akuntan</option>
                        <option value="owner" {'selected' if role == 'owner' else ''}>Owner</option>
                        <option value="karyawan" {'selected' if role == 'karyawan' else ''}>Karyawan</option>
                    </select>
                </div>
                <button type="submit" class="btn">Daftar</button>
            </form>
            <div class="links">
                <p>Sudah punya akun? <a href="/login">Login di sini</a></p>
                <p><a href="/">← Kembali ke Halaman Utama</a></p>
            </div>
        </div>
    </body>
    </html>
    """
    return html

def generate_verify_email_page(token):
    """Generate halaman verifikasi email"""
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Verifikasi Email - Geboy Mujair</title>
        {generate_base_style()}
    </head>
    <body>
        <div class="container">
            <div class="logo">✉️</div>
            <h1>Buat Akun</h1>
            <p class="subtitle">Email Anda telah diverifikasi! Silakan buat username dan password.</p>
            {flash_html}
            <form method="POST">
                <div class="form-group">
                    <label for="username">Username</label>
                    <input type="text" id="username" name="username" required placeholder="Minimal 3 karakter" minlength="3">
                </div>
                <div class="password-requirements">
                    <h3>Ketentuan Password:</h3>
                    <ul>
                        <li>8-20 karakter</li>
                        <li>Minimal 1 huruf besar (A-Z)</li>
                        <li>Minimal 1 huruf kecil (a-z)</li>
                        <li>Minimal 1 angka (0-9)</li>
                        <li>Minimal 1 karakter khusus (!@#$%^&*...)</li>
                    </ul>
                </div>
                <div class="form-group">
                    <label for="password">Password</label>
                    <input type="password" id="password" name="password" required placeholder="Masukkan password">
                </div>
                <div class="form-group">
                    <label for="confirm_password">Konfirmasi Password</label>
                    <input type="password" id="confirm_password" name="confirm_password" required placeholder="Ulangi password">
                </div>
                <button type="submit" class="btn">Buat Akun</button>
            </form>
        </div>
    </body>
    </html>
    """
    return html

def generate_login_page():
    """Generate halaman login"""
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Login - Geboy Mujair</title>
        {generate_base_style()}
    </head>
    <body>
        <div class="container">
            <div class="logo">🐟</div>
            <h1>Login</h1>
            {flash_html}
            <form method="POST" action="/login">
                <div class="form-group">
                    <label for="username">Username</label>
                    <input type="text" id="username" name="username" required placeholder="Masukkan username">
                </div>
                <div class="form-group">
                    <label for="password">Password</label>
                    <input type="password" id="password" name="password" required placeholder="Masukkan password">
                </div>
                <button type="submit" class="btn">Login</button>
            </form>
            <div class="links">
                <a href="/forgot-password">Lupa Password?</a>
                <p>Belum punya akun? <a href="/register">Daftar di sini</a></p>
                <a href="/">← Kembali ke Halaman Utama</a>
            </div>
        </div>
    </body>
    </html>
    """
    return html

def generate_forgot_password_page():
    """Generate halaman lupa password"""
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Lupa Password - Geboy Mujair</title>
        {generate_base_style()}
    </head>
    <body>
        <div class="container">
            <div class="logo">🔑</div>
            <h1>Lupa Password</h1>
            <p class="subtitle">Masukkan email Anda dan kami akan mengirimkan link untuk reset password.</p>
            {flash_html}
            <form method="POST" action="/forgot-password">
                <div class="form-group">
                    <label for="email">Email</label>
                    <input type="email" id="email" name="email" required placeholder="email@example.com">
                </div>
                <button type="submit" class="btn">Kirim Link Reset</button>
            </form>
            <div class="links">
                <p><a href="/login">← Kembali ke Login</a></p>
            </div>
        </div>
    </body>
    </html>
    """
    return html

def generate_reset_password_page(token):
    """Generate halaman reset password"""
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Reset Password - Geboy Mujair</title>
        {generate_base_style()}
    </head>
    <body>
        <div class="container">
            <div class="logo">🔒</div>
            <h1>Reset Password</h1>
            <p class="subtitle">Buat password baru untuk akun Anda.</p>
            {flash_html}
            <form method="POST">
                <div class="password-requirements">
                    <h3>Ketentuan Password:</h3>
                    <ul>
                        <li>8-20 karakter</li>
                        <li>Minimal 1 huruf besar (A-Z)</li>
                        <li>Minimal 1 huruf kecil (a-z)</li>
                        <li>Minimal 1 angka (0-9)</li>
                        <li>Minimal 1 karakter khusus (!@#$%^&*...)</li>
                    </ul>
                </div>
                <div class="form-group">
                    <label for="password">Password Baru</label>
                    <input type="password" id="password" name="password" required placeholder="Masukkan password baru">
                </div>
                <div class="form-group">
                    <label for="confirm_password">Konfirmasi Password</label>
                    <input type="password" id="confirm_password" name="confirm_password" required placeholder="Ulangi password baru">
                </div>
                <button type="submit" class="btn">Reset Password</button>
            </form>
        </div>
    </body>
    </html>
    """
    return html

# ============== DASHBOARD GENERATORS ==============

def generate_kasir_dashboard():
    """Generate dashboard kasir dengan fitur POS"""
    username = session.get('username', 'User')
    
    # Ambil transaksi hari ini
    today = datetime.now().strftime('%Y-%m-%d')
    transactions = get_transactions(start_date=today, end_date=today)
    total_sales = sum(float(t['total_amount']) for t in transactions)
    total_transactions = len(transactions)
    
    # Hitung total item terjual
    total_items = 0
    for trans in transactions:
        items = json.loads(trans['items']) if isinstance(trans['items'], str) else trans['items']
        total_items += sum(item['quantity'] for item in items)
    
    # Rata-rata per transaksi
    avg_transaction = total_sales / total_transactions if total_transactions > 0 else 0
    
    transactions_html = ""
    for trans in transactions[:10]:  # 10 transaksi terakhir
        items = json.loads(trans['items']) if isinstance(trans['items'], str) else trans['items']
        items_str = ", ".join([f"{item['name']} ({item['quantity']}kg)" for item in items])
        date_obj = datetime.fromisoformat(trans['date'].replace('Z', '+00:00'))
        transactions_html += f"""
        <tr>
            <td class="text-center">{trans['transaction_code']}</td>
            <td>{date_obj.strftime('%d/%m/%Y %H:%M:%S')}</td>
            <td>{items_str}</td>
            <td class="text-right">{format_rupiah(trans['total_amount'])}</td>
            <td class="text-center">
                <button class="btn-sm btn-info" onclick="viewReceipt('{trans['transaction_code']}')">📄 Struk</button>
            </td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dashboard Kasir - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">💰</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Kasir</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/kasir" class="active"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/kasir/pos"><span class="icon">🛒</span> Point of Sale</a></li>
                    <li><a href="/kasir/transactions"><span class="icon">📋</span> Riwayat Transaksi</a></li>
                    <li><a href="/kasir/daily-report"><span class="icon">📊</span> Laporan Harian</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Dashboard Kasir</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">💵</div>
                        <div class="stat-value">{format_rupiah(total_sales)}</div>
                        <div class="stat-label">Penjualan Hari Ini</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📝</div>
                        <div class="stat-value">{total_transactions}</div>
                        <div class="stat-label">Transaksi Hari Ini</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">🐟</div>
                        <div class="stat-value">{total_items:.1f} kg</div>
                        <div class="stat-label">Ikan Terjual</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📈</div>
                        <div class="stat-value">{format_rupiah(avg_transaction)}</div>
                        <div class="stat-label">Rata-rata Transaksi</div>
                    </div>
                </div>
                
                <div class="content-section">
                    <h2>⚡ Quick Actions</h2>
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px;">
                        <a href="/kasir/pos" class="btn-sm btn-success btn-block">🛒 Buat Transaksi Baru</a>
                        <a href="/kasir/transactions" class="btn-sm btn-info btn-block">📋 Lihat Riwayat</a>
                        <a href="/kasir/daily-report" class="btn-sm btn-primary btn-block">📊 Laporan Harian</a>
                    </div>
                </div>
                
                <div class="content-section">
                    <h2>📝 Transaksi Terbaru Hari Ini</h2>
                    <table>
                        <thead>
                            <tr>
                                <th class="text-center">Kode</th>
                                <th>Tanggal & Waktu</th>
                                <th>Item</th>
                                <th class="text-right">Total</th>
                                <th class="text-center">Aksi</th>
                            </tr>
                        </thead>
                        <tbody>
                            {transactions_html if transactions_html else '<tr><td colspan="5" class="text-center">Belum ada transaksi hari ini</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        
        <script>
        function viewReceipt(code) {{
            window.open('/kasir/receipt/' + code, '_blank');
        }}
        </script>
    </body>
    </html>
    """
    return html

def generate_sidebar(role, username, active_page='dashboard'):
    role_info = {
        'kasir': {'icon': '💰', 'title': 'Kasir'},
        'akuntan': {'icon': '📊', 'title': 'Akuntan'},
        'owner': {'icon': '👔', 'title': 'Owner'},
        'karyawan': {'icon': '👷', 'title': 'Karyawan'}
    }
    
    info = role_info.get(role, role_info['kasir'])
    
    menus = {
        'kasir': [
            ('dashboard', '🏠', 'Dashboard', '/dashboard/kasir'),
            ('pos', '🛒', 'Point of Sale', '/kasir/pos'),
            ('transactions', '📋', 'Riwayat Transaksi', '/kasir/transactions'),
            ('daily', '📊', 'Laporan Harian', '/kasir/daily-report'),
        ],
        'akuntan': [
            ('dashboard', '🏠', 'Dashboard', '/dashboard/akuntan'),
            ('accounts', '📋', 'Daftar Akun', '/akuntan/accounts'),
            ('journal-gj', '📝', 'Jurnal Umum', '/akuntan/journal-gj'),
            ('manual-transaction', '➕', 'Transaksi Manual', '/akuntan/manual-transaction'),
            ('inventory-card', '📦', 'Inventory Card', '/akuntan/inventory-card'),
            ('adjustment-journal', '🔧', 'Penyesuaian', '/akuntan/adjustment-journal'),
            ('closing-journal', '🔒', 'Penutupan', '/akuntan/closing-journal'),
            ('reversing-journal', '🔄', 'Pembalikan', '/akuntan/reversing-journal'),
            ('assets', '🏢', 'Aset', '/akuntan/assets'),
            ('ledger', '📚', 'Buku Besar', '/akuntan/ledger'),
            ('trial-balance', '⚖️', 'NS', '/akuntan/trial-balance'),
            ('adjusted-trial-balance', '✅', 'NS Penyesuaian', '/akuntan/adjusted-trial-balance'),
            ('worksheet', '📊', 'Neraca Lajur', '/akuntan/worksheet'),
            ('financial-statements', '💼', 'Lap. Keuangan', '/akuntan/financial-statements'),
            ('cash-flow-statement', '💰', 'Arus Kas', '/akuntan/cash-flow-statement'),
            ('post-closing-trial-balance', '📄', 'NS Penutupan', '/akuntan/post-closing-trial-balance'),
        ],
        'karyawan': [
            ('dashboard', '🏠', 'Dashboard', '/dashboard/karyawan'),
            ('purchase', '🛒', 'Pembelian Baru', '/karyawan/purchase'),
            ('history', '📋', 'Riwayat Pembelian', '/karyawan/purchase-history'),
        ],
        'owner': [
            ('dashboard', '🏠', 'Dashboard', '/dashboard/owner'),
            ('analytics', '📈', 'Analytics', '/owner/analytics'),
            ('financial', '📊', 'Laporan Keuangan', '/owner/financial-reports'),
            ('users', '👥', 'Manajemen User', '/owner/users'),
        ]
    }
    
    info = role_info.get(role, role_info['kasir'])
    menu_items = menus.get(role, [])
    
    menu_html = ""
    for menu_id, icon, label, url in menu_items:
        active_class = 'active' if active_page == menu_id else ''
        menu_html += f'''
        <li><a href="{url}" class="{active_class}">
            <span class="icon">{icon}</span> {label}
        </a></li>
        '''
    
    menu_html += '<li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>'
    
    return f"""
    <div class="sidebar">
        <div class="sidebar-header">
            <div class="sidebar-logo">🐟</div>
            <div class="sidebar-title">Geboy Mujair</div>
            <div class="sidebar-subtitle">Sistem Akuntansi</div>
        </div>
        
        <div class="sidebar-user">
            <div class="sidebar-user-icon">{info['icon']}</div>
            <div class="sidebar-user-name">{username}</div>
            <div class="sidebar-user-role">{info['title']}</div>
        </div>
        
        <ul class="sidebar-menu">
            {menu_html}
        </ul>
    </div>
    """

# ============== ROUTES - AUTH ==============

@app.route('/')
def index():
    return generate_index_page()
    
def generate_kasir_pos():
    """Generate halaman POS untuk kasir"""
    username = session.get('username', 'User')

    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Point of Sale - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>

                <div class="sidebar-user">
                    <div class="sidebar-user-icon">💰</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Kasir</div>
                </div>

                <ul class="sidebar-menu">
                    <li><a href="/dashboard/kasir"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/kasir/pos" class="active"><span class="icon">🛒</span> Point of Sale</a></li>
                    <li><a href="/kasir/transactions"><span class="icon">📋</span> Riwayat Transaksi</a></li>
                    <li><a href="/kasir/daily-report"><span class="icon">📊</span> Laporan Harian</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>

            <div class="main-content">
                <div class="top-bar">
                    <h1>Point of Sale</h1>
                    <div class="date-time" id="datetime"></div>
                </div>

                <div class="content-section">
                    <h2>🛒 Tambah Item</h2>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Nama Ikan</label>
                            <input type="text" id="itemName" value="Ikan Mujair" readonly>
                        </div>
                        <div class="form-group">
                            <label>Jumlah (Kg)</label>
                            <input type="number" id="itemQty" min="0.5" step="0.5" placeholder="0.5">
                        </div>
                        <div class="form-group">
                            <label>Harga/Kg</label>
                            <input type="text" id="itemPrice" value="Rp30.000,00">
                        </div>
                        <div class="form-group" style="display: flex; align-items: flex-end;">
                            <button class="btn-sm btn-success btn-block" onclick="addItem()">➕ Tambah</button>
                        </div>
                    </div>

                    <div class="cart-items" id="cartItems">
                        <p style="text-align: center; color: #999;">Belum ada item</p>
                    </div>

                    <div class="cart-total">
                        <p>Total Pembayaran:</p>
                        <h3 id="totalAmount">Rp0,00</h3>
                    </div>

                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-top: 20px;">
                        <button class="btn-sm btn-danger btn-block" onclick="clearCart()">🗑️ Kosongkan Keranjang</button>
                        <button class="btn-sm btn-success btn-block" onclick="processTransaction()">💵 Proses Pembayaran</button>
                    </div>
                </div>
            </div>
        </div>

        <script>
        let cart = [];

        function formatRupiah(amount) {{
            return 'Rp' + amount.toFixed(2)
                .replace(/\\d(?=(\\d{{3}})+\\.)/g, '$&,')
                .replace('.', ',');
        }}

        function parseRupiah(str) {{
            return parseFloat(
                str.replace(/Rp/g, '')
                   .replace(/\\./g, '')
                   .replace(',', '.')
            ) || 0;
        }}

        function addItem() {{
            const name = document.getElementById('itemName').value;
            const qty = parseFloat(document.getElementById('itemQty').value);
            const price = parseRupiah(document.getElementById('itemPrice').value);

            if (!qty || qty <= 0) {{
                alert('Masukkan jumlah yang valid!');
                return;
            }}

            if (!price || price <= 0) {{
                alert('Masukkan harga yang valid!');
                return;
            }}

            cart.push({{
                name: name,
                quantity: qty,
                price: price,
                subtotal: qty * price
            }});

            updateCart();
            document.getElementById('itemQty').value = '';
        }}

        function removeItem(index) {{
            cart.splice(index, 1);
            updateCart();
        }}

        function updateCart() {{
            const cartDiv = document.getElementById('cartItems');

            if (cart.length === 0) {{
                cartDiv.innerHTML = '<p style="text-align: center; color: #999;">Belum ada item</p>';
                document.getElementById('totalAmount').textContent = 'Rp0,00';
                return;
            }}

            let html = '';
            let total = 0;

            cart.forEach((item, index) => {{
                total += item.subtotal;
                html += `
                    <div class="cart-item">
                        <div>
                            <strong>${{item.name}}</strong><br>
                            <small>${{item.quantity}} kg × ${{formatRupiah(item.price)}} = ${{formatRupiah(item.subtotal)}}</small>
                        </div>
                        <button class="btn-sm btn-danger" onclick="removeItem(${{index}})">🗑️</button>
                    </div>
                `;
            }});

            cartDiv.innerHTML = html;
            document.getElementById('totalAmount').textContent = formatRupiah(total);
        }}

        function clearCart() {{
            if (cart.length === 0) return;
            if (confirm('Kosongkan keranjang?')) {{
                cart = [];
                updateCart();
            }}
        }}

        function processTransaction() {{
            if (cart.length === 0) {{
                alert('Keranjang masih kosong!');
                return;
            }}

            if (!confirm('Proses pembayaran?')) return;

            fetch('/kasir/process', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ items: cart }})
            }})
            .then(res => res.json())
            .then(data => {{
                if (data.success) {{
                    alert('Transaksi berhasil! Kode: ' + data.transaction_code);
                    window.open('/kasir/receipt/' + data.transaction_code, '_blank');
                    cart = [];
                    updateCart();
                }} else {{
                    alert('Error: ' + data.message);
                }}
            }})
            .catch(err => {{
                alert('Terjadi kesalahan: ' + err);
            }});
        }}
        </script>

    </body>
    </html>
    """

    return html

@app.route('/kasir/transactions')
def kasir_transactions():
    """Halaman riwayat transaksi kasir"""
    if 'username' not in session or session.get('role') != 'kasir':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Filter
    period = request.args.get('period', 'today')
    today = datetime.now()
    
    if period == 'today':
        start_date = today.strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')
    elif period == 'week':
        start_date = (today - timedelta(days=7)).strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')
    elif period == 'month':
        start_date = today.replace(day=1).strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')
    else:
        start_date = request.args.get('start_date', today.strftime('%Y-%m-%d'))
        end_date = request.args.get('end_date', today.strftime('%Y-%m-%d'))
    
    transactions = get_transactions(start_date, end_date)
    total_sales = sum(float(t['total_amount']) for t in transactions)
    
    transactions_html = ""
    for trans in transactions:
        items = json.loads(trans['items']) if isinstance(trans['items'], str) else trans['items']
        items_str = ", ".join([f"{item['name']} ({item['quantity']}kg)" for item in items])
        date_obj = datetime.fromisoformat(trans['date'].replace('Z', '+00:00'))
        
        transactions_html += f"""
        <tr>
            <td class="text-center">{trans['transaction_code']}</td>
            <td>{date_obj.strftime('%d/%m/%Y %H:%M:%S')}</td>
            <td>{items_str}</td>
            <td class="text-right">{format_rupiah(trans['total_amount'])}</td>
            <td class="text-center">
                <div class="btn-group">
                    <button class="btn-sm btn-info" onclick="viewReceipt('{trans['transaction_code']}')">📄 Struk</button>
                    <a href="/kasir/edit-transaction/{trans['transaction_code']}" class="btn-sm btn-warning">✏️ Edit</a>
                    <button class="btn-sm btn-danger" onclick="deleteTransaction('{trans['transaction_code']}')">🗑️ Hapus</button>
                </div>
            </td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Riwayat Transaksi - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">💰</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Kasir</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/kasir"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/kasir/pos"><span class="icon">🛒</span> Point of Sale</a></li>
                    <li><a href="/kasir/transactions" class="active"><span class="icon">📋</span> Riwayat Transaksi</a></li>
                    <li><a href="/kasir/daily-report"><span class="icon">📊</span> Laporan Harian</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Riwayat Transaksi</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <h2>🔍 Filter Transaksi</h2>
                    <form method="GET" class="form-row">
                        <div class="form-group">
                            <label>Periode</label>
                            <select name="period" onchange="this.form.submit()">
                                <option value="today" {'selected' if period == 'today' else ''}>Hari Ini</option>
                                <option value="week" {'selected' if period == 'week' else ''}>7 Hari Terakhir</option>
                                <option value="month" {'selected' if period == 'month' else ''}>Bulan Ini</option>
                                <option value="custom" {'selected' if period == 'custom' else ''}>Custom</option>
                            </select>
                        </div>
                        {f'''
                        <div class="form-group">
                            <label>Dari Tanggal</label>
                            <input type="date" name="start_date" value="{start_date}">
                        </div>
                        <div class="form-group">
                            <label>Sampai Tanggal</label>
                            <input type="date" name="end_date" value="{end_date}">
                        </div>
                        <div class="form-group" style="display: flex; align-items: flex-end;">
                            <button type="submit" class="btn-sm btn-primary btn-block">🔍 Filter</button>
                        </div>
                        ''' if period == 'custom' else ''}
                    </form>
                </div>
                
                <div class="content-section">
                    <h2>📊 Ringkasan</h2>
                    <div class="stats-grid" style="grid-template-columns: repeat(2, 1fr);">
                        <div class="stat-card">
                            <div class="stat-icon">📝</div>
                            <div class="stat-value">{len(transactions)}</div>
                            <div class="stat-label">Total Transaksi</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-icon">💵</div>
                            <div class="stat-value">{format_rupiah(total_sales)}</div>
                            <div class="stat-label">Total Penjualan</div>
                        </div>
                    </div>
                </div>
                
                <div class="content-section">
                    <h2>📋 Daftar Transaksi</h2>
                    <table>
                        <thead>
                            <tr>
                                <th class="text-center">Kode</th>
                                <th>Tanggal & Waktu</th>
                                <th>Item</th>
                                <th class="text-right">Total</th>
                                <th class="text-center">Aksi</th>
                            </tr>
                        </thead>
                        <tbody>
                            {transactions_html if transactions_html else '<tr><td colspan="5" class="text-center">Tidak ada transaksi</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        
        <script>
        function viewReceipt(code) {{
            window.open('/kasir/receipt/' + code, '_blank');
        }}
        
        function deleteTransaction(code) {{
            if (confirm('Yakin ingin menghapus transaksi ' + code + '?')) {{
                fetch('/kasir/delete-transaction/' + code, {{
                    method: 'DELETE'
                }})
                .then(res => res.json())
                .then(data => {{
                    if (data.success) {{
                        alert('Transaksi berhasil dihapus!');
                        location.reload();
                    }} else {{
                        alert('Error: ' + data.message);
                    }}
                }});
            }}
        }}
        </script>
    </body>
    </html>
    """
    
    return html

@app.route('/kasir/receipt/<transaction_code>')
def kasir_receipt(transaction_code):
    """Generate dan tampilkan struk"""
    if 'username' not in session or session.get('role') != 'kasir':
        return redirect(url_for('login'))
    
    try:
        response = supabase.table('transactions').select('*').eq('transaction_code', transaction_code).execute()
        if not response.data:
            return "Transaksi tidak ditemukan", 404
        
        transaction = response.data[0]
        items = json.loads(transaction['items']) if isinstance(transaction['items'], str) else transaction['items']
        date_obj = datetime.fromisoformat(transaction['date'].replace('Z', '+00:00'))
        
        items_html = ""
        for item in items:
            items_html += f"""
            <div class="receipt-item">
                <div>
                    <div>{item['name']}</div>
                    <div style="font-size: 11px;">{item['quantity']}kg x {format_rupiah(item['price'])}</div>
                </div>
                <div>{format_rupiah(item['subtotal'])}</div>
            </div>
            """
        
        html = f"""
        <!DOCTYPE html>
        <html lang="id">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Struk - {transaction_code}</title>
            {generate_dashboard_style()}
            <style>
                @media print {{
                    body {{ margin: 0; padding: 20px; }}
                    .no-print {{ display: none !important; }}
                }}
            </style>
        </head>
        <body>
            <div style="max-width: 400px; margin: 20px auto;">
                <button onclick="window.print()" class="btn-sm btn-primary no-print" style="margin-bottom: 20px; width: 100%;">🖨️ Cetak Struk</button>
                
                <div class="receipt">
                    <div class="receipt-header">
                        <div class="receipt-title">GEBOY MUJAIR</div>
                        <div class="receipt-address">
                            Sidodadi RT 4 RW 3<br>
                            Karanggedong, Ngadirejo<br>
                            Temanggung, Jawa Tengah<br>
                            Telp: 0293-XXXXXXX
                        </div>
                    </div>
                    
                    <div class="receipt-info">
                        <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                            <span>No. Transaksi:</span>
                            <span><strong>{transaction_code}</strong></span>
                        </div>
                        <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                            <span>Tanggal:</span>
                            <span>{date_obj.strftime('%d/%m/%Y')}</span>
                        </div>
                        <div style="display: flex; justify-content: space-between; margin-bottom: 5px;">
                            <span>Waktu:</span>
                            <span>{date_obj.strftime('%H:%M:%S')}</span>
                        </div>
                        <div style="display: flex; justify-content: space-between;">
                            <span>Kasir:</span>
                            <span>{transaction.get('cashier_username', '-')}</span>
                        </div>
                    </div>
                    
                    <div class="receipt-line"></div>
                    
                    <div class="receipt-items">
                        {items_html}
                    </div>
                    
                    <div class="receipt-line"></div>
                    
                    <div class="receipt-total">
                        <span>TOTAL:</span>
                        <span>{format_rupiah(transaction['total_amount'])}</span>
                    </div>
                    
                    <div class="receipt-total" style="font-size: 14px; font-weight: normal;">
                        <span>Tunai:</span>
                        <span>{format_rupiah(transaction['total_amount'])}</span>
                    </div>
                    
                    <div class="receipt-total" style="font-size: 14px; font-weight: normal;">
                        <span>Kembali:</span>
                        <span>Rp0,00</span>
                    </div>
                    
                    <div class="receipt-footer">
                        <p>Terima kasih atas kunjungan Anda!</p>
                        <p>Barang yang sudah dibeli tidak dapat dikembalikan</p>
                        <p style="margin-top: 10px;">www.geboymujair.com</p>
                    </div>
                </div>
                
                <button onclick="window.close()" class="btn-sm btn-secondary no-print" style="margin-top: 20px; width: 100%;">✖️ Tutup</button>
            </div>
        </body>
        </html>
        """
        
        return html
        
    except Exception as e:
        return f"Error: {str(e)}", 500

@app.route('/kasir/delete-transaction/<transaction_code>', methods=['DELETE'])
def kasir_delete_transaction(transaction_code):
    if 'username' not in session or session.get('role') != 'kasir':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        # Hapus jurnal entries dulu
        supabase.table('journal_entries').delete().eq('ref_code', transaction_code).execute()
        
        # Baru hapus transaksi
        supabase.table('transactions').delete().eq('transaction_code', transaction_code).execute()
        
        return jsonify({'success': True, 'message': 'Transaksi dan jurnal berhasil dihapus'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/kasir/edit-transaction/<transaction_code>', methods=['GET', 'POST'])
def kasir_edit_transaction(transaction_code):
    if 'username' not in session or session.get('role') != 'kasir':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            data = request.get_json()
            items = data.get('items', [])
            total_amount = sum(item['subtotal'] for item in items)
            
            # Update transaction
            supabase.table('transactions').update({
                'items': json.dumps(items),
                'total_amount': float(total_amount)
            }).eq('transaction_code', transaction_code).execute()
            
            # Update journal entries
            supabase.table('journal_entries').delete().eq('ref_code', transaction_code).execute()

            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})
    
    # GET - tampilkan form edit
    try:
        response = supabase.table('transactions').select('*').eq('transaction_code', transaction_code).execute()
        if not response.data:
            flash('Transaksi tidak ditemukan', 'error')
            return redirect(url_for('kasir_transactions'))
        
        transaction = response.data[0]
        items = json.loads(transaction['items']) if isinstance(transaction['items'], str) else transaction['items']
        
        username = session.get('username')
        
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Edit Transaksi</title>
            {generate_dashboard_style()}
        </head>
        <body>
            <div class="dashboard-container">
                {generate_sidebar('kasir', username, 'transactions')}
                <div class="main-content">
                    <div class="top-bar">
                        <h1>Edit Transaksi {transaction_code}</h1>
                    </div>
                    <div class="content-section">
                        <div id="editCart"></div>
                        <button class="btn-sm btn-success btn-block" onclick="saveEdit()">💾 Simpan</button>
                        <a href="/kasir/transactions" class="btn-sm btn-secondary btn-block">Batal</a>
                    </div>
                </div>
            </div>
            <script>
                let cart = {json.dumps(items)};
                
                function renderCart() {{
                    let html = '';
                    cart.forEach((item, i) => {{
                        html += `
                            <div class="cart-item">
                                <input type="text" value="${{item.name}}" onchange="cart[${{i}}].name = this.value">
                                <input type="number" value="${{item.quantity}}" step="0.5" onchange="updateItem(${{i}}, this.value, 'quantity')">
                                <input type="number" value="${{item.price}}" onchange="updateItem(${{i}}, this.value, 'price')">
                                <button onclick="removeItem(${{i}})">🗑️</button>
                            </div>
                        `;
                    }});
                    document.getElementById('editCart').innerHTML = html;
                }}
                
                function updateItem(i, val, field) {{
                    cart[i][field] = parseFloat(val);
                    cart[i].subtotal = cart[i].quantity * cart[i].price;
                    renderCart();
                }}
                
                function removeItem(i) {{
                    cart.splice(i, 1);
                    renderCart();
                }}
                
                function saveEdit() {{
                    fetch('/kasir/edit-transaction/{transaction_code}', {{
                        method: 'POST',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{items: cart}})
                    }}).then(r => r.json()).then(d => {{
                        if(d.success) {{
                            alert('Berhasil');
                            window.location.href = '/kasir/transactions';
                        }}
                    }});
                }}
                
                renderCart();
            </script>
        </body>
        </html>
        """
        return html
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
        return redirect(url_for('kasir_transactions'))

@app.route('/kasir/daily-report')
def kasir_daily_report():
    if 'username' not in session or session.get('role') != 'kasir':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # ================================
    # FILTER PERIODE
    # ================================
    period = request.args.get('period', 'today')
    today = datetime.now()
    
    if period == 'today':
        start_date = today.strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')
        title = 'Hari Ini'
    elif period == 'week':
        start_date = (today - timedelta(days=today.weekday())).strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')
        title = 'Minggu Ini'
    elif period == 'month':
        start_date = today.replace(day=1).strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')
        title = 'Bulan Ini'
    else:
        start_date = today.strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')
        title = 'Hari Ini'
    
    # ================================
    # DATA TRANSAKSI
    # ================================
    transactions = get_transactions(start_date=start_date, end_date=end_date)
    total_sales = sum(float(t['total_amount']) for t in transactions)
    total_items = sum(sum(item['quantity'] for item in (json.loads(t['items']) if isinstance(t['items'], str) else t['items'])) for t in transactions)

    # ================================
    # GRAFIK PENJUALAN PER TANGGAL
    # ================================
    sales_by_date = {}
    for trans in transactions:
        date_obj = datetime.fromisoformat(trans['date'].replace('Z', '+00:00'))
        date_key = date_obj.strftime('%Y-%m-%d')
        sales_by_date[date_key] = sales_by_date.get(date_key, 0) + float(trans['total_amount'])

    chart_data = [{'date': k, 'sales': v} for k, v in sorted(sales_by_date.items())]
    
    # ================================
    # DROPDOWN FILTER
    # ================================
    filter_html = f"""
    <div class="form-group" style="margin-bottom:20px;">
        <label><b>Periode Laporan</b></label>
        <select onchange="window.location.href='/kasir/daily-report?period=' + this.value">
            <option value="today" {'selected' if period=='today' else ''}>Hari Ini</option>
            <option value="week" {'selected' if period=='week' else ''}>Minggu Ini</option>
            <option value="month" {'selected' if period=='month' else ''}>Bulan Ini</option>
        </select>
    </div>
    """

    # ================================
    # HTML DASHBOARD + CHART
    # ================================
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Laporan Kasir - {title}</title>
        {generate_dashboard_style()}
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>

    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">💰</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Kasir</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/kasir"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/kasir/pos"><span class="icon">🛒</span> Point of Sale</a></li>
                    <li><a href="/kasir/transactions"><span class="icon">📋</span> Riwayat Transaksi</a></li>
                    <li><a href="/kasir/daily-report" class="active"><span class="icon">📊</span> Laporan Kasir</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>

            <div class="main-content">
                <div class="top-bar">
                    <h1>Laporan Kasir - {title}</h1>
                    <div class="date-time" id="datetime"></div>
                </div>

                {filter_html}

                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">📝</div>
                        <div class="stat-value">{len(transactions)}</div>
                        <div class="stat-label">Jumlah Transaksi</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">💵</div>
                        <div class="stat-value">{format_rupiah(total_sales)}</div>
                        <div class="stat-label">Total Penjualan</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">🐟</div>
                        <div class="stat-value">{total_items:.1f} kg</div>
                        <div class="stat-label">Total Ikan Terjual</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📈</div>
                        <div class="stat-value">{format_rupiah(total_sales / len(transactions) if transactions else 0)}</div>
                        <div class="stat-label">Rata-rata per Transaksi</div>
                    </div>
                </div>

                <div class="content-section">
                    <h2>📈 Grafik Penjualan</h2>
                    <canvas id="salesChart" style="max-height: 400px;"></canvas>
                </div>

                <div class="content-section no-print">
                    <button onclick="window.print()" class="btn-sm btn-primary btn-block">
                        🖨️ Cetak Laporan
                    </button>
                </div>
            </div>
        </div>

        <script>
        const ctx = document.getElementById('salesChart').getContext('2d');
        const chartData = {json.dumps(chart_data)};

        new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: chartData.map(d => d.date),
                datasets: [{{
                    label: 'Penjualan (Rp)',
                    data: chartData.map(d => d.sales),
                    backgroundColor: 'rgba(102, 126, 234, 0.2)',
                    borderColor: 'rgba(102, 126, 234, 1)',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.4
                }}]
            }},
            options: {{
                responsive: true,
                scales: {{
                    y: {{
                        beginAtZero: true,
                        ticks: {{
                            callback: function(value) {{
                                return 'Rp' + value.toLocaleString('id-ID');
                            }}
                        }}
                    }}
                }}
            }}
        }});
        </script>

    </body>
    </html>
    """

    return html

@app.route('/kasir/penjualan/submit', methods=['POST'])
def kasir_submit_penjualan():
    """Submit penjualan dan auto-update inventory + jurnal"""
    if 'username' not in session or session.get('role') != 'kasir':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        
        # Data penjualan
        date = data.get('date')
        customer = data.get('customer', 'Umum')
        quantity = float(data.get('quantity', 0))
        sale_price = float(data.get('sale_price', 0))  # Harga jual
        description = data.get('description', '')
        cashier = session.get('username')
        
        # Validasi
        if quantity <= 0:
            return jsonify({'success': False, 'message': 'Quantity harus > 0'}), 400
        
        # Cek stok tersedia
        last_inventory = supabase.table('inventory_card')\
            .select('balance_quantity')\
            .order('id', desc=True)\
            .limit(1)\
            .execute()
        
        current_stock = last_inventory.data[0]['balance_quantity'] if last_inventory.data else 0
        
        if current_stock < quantity:
            return jsonify({
                'success': False, 
                'message': f'Stok tidak cukup! Tersedia: {current_stock} kg'
            }), 400
        
        # Ambil HPP dari inventory terakhir
        last_hpp = supabase.table('inventory_card')\
            .select('unit_price')\
            .order('id', desc=True)\
            .limit(1)\
            .execute()
        
        unit_price_hpp = last_hpp.data[0]['unit_price'] if last_hpp.data else 0
        
        # Process penjualan lengkap
        result = process_sale_transaction(
            date=date,
            customer=customer,
            quantity=quantity,
            unit_price=unit_price_hpp,  # HPP
            sale_price=sale_price,       # Harga jual
            description=description,
            cashier=cashier
        )
        
        if result['success']:
            return jsonify(result), 200
        else:
            return jsonify(result), 500
        
    except Exception as e:
        print(f"❌ Error submit penjualan: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/karyawan/purchase', methods=['GET', 'POST'])
def karyawan_purchase():
    """Form pembelian karyawan"""
    if 'username' not in session or session.get('role') != 'karyawan':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            item_type = request.form.get('item_type')
            item_name = request.form.get('item_name')
            quantity = float(request.form.get('quantity'))
            unit_price_str = request.form.get('unit_price')
            unit_price = parse_rupiah(unit_price_str)
            
            # Validasi input
            if not item_type or not item_name:
                flash('Jenis item dan nama item harus diisi!', 'error')
                return redirect(url_for('karyawan_purchase'))
            
            if quantity <= 0:
                flash('Jumlah harus lebih dari 0!', 'error')
                return redirect(url_for('karyawan_purchase'))
            
            if unit_price <= 0:
                flash('Harga satuan harus lebih dari 0!', 'error')
                return redirect(url_for('karyawan_purchase'))
            
            total_amount = quantity * unit_price
            
            # Simpan pembelian - PERBAIKAN DI SINI
            purchase = create_purchase(
                item_type=item_type,
                item_name=item_name,
                quantity=quantity,
                unit_price=unit_price,
                total_amount=total_amount,
                employee_username=session.get('username')  # Gunakan .get() bukan ()
            )
            
            if purchase:
                flash('Pembelian berhasil dicatat!', 'success')
                return redirect(url_for('karyawan_purchase_history'))
            else:
                flash('Gagal menyimpan pembelian!', 'error')
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
    
    # ... sisa kode tetap sama
    
    username = session.get('username', 'User')
    
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Form Pembelian - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">👷</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Karyawan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/karyawan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/karyawan/purchase" class="active"><span class="icon">🛒</span> Pembelian</a></li>
                    <li><a href="/karyawan/purchase-history"><span class="icon">📋</span> Riwayat</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Form Pembelian</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <h2>🛒 Catat Pembelian Baru</h2>
                    {flash_html}
                    <form method="POST" enctype="multipart/form-data">
                        <div class="form-row">
                            <div class="form-group">
                                <label>Jenis Item *</label>
                                <select name="item_type" required id="itemType">
                                    <option value="">-- Pilih Jenis --</option>
                                    <option value="bibit">🐟 Bibit Ikan Mujair</option>
                                    <option value="perlengkapan">📦 Perlengkapan (Pakan, Obat, Vitamin)</option>
                                    <option value="peralatan">🔧 Peralatan</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Nama Item *</label>
                                <input type="text" name="item_name" required placeholder="Contoh: Pakan Ikan Apung 1kg">
                            </div>
                        </div>
                        
                        <div class="form-row">
                            <div class="form-group">
                                <label>Jumlah/Kuantitas *</label>
                                <input type="number" name="quantity" step="0.01" min="0.01" required placeholder="0" id="quantity">
                            </div>
                            <div class="form-group">
                                <label>Harga Satuan *</label>
                                <input type="text" name="unit_price" required placeholder="Rp0,00" id="unitPrice">
                            </div>
                            <div class="form-group">
                                <label>Total Harga</label>
                                <input type="text" id="totalPrice" readonly placeholder="Rp0,00" style="background: #f0f0f0;">
                            </div>
                        </div>
                        
                        <div class="form-group">
                            <label>Upload Bukti Nota/Struk (Opsional)</label>
                            <input type="file" name="receipt_image" accept="image/*" style="padding: 8px;">
                            <small style="color: #666; display: block; margin-top: 5px;">Format: JPG, PNG, PDF. Max 2MB</small>
                        </div>
                        
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-top: 20px;">
                            <a href="/karyawan/purchase-history" class="btn-sm btn-secondary btn-block">↩️ Kembali</a>
                            <button type="submit" class="btn-sm btn-success btn-block">💾 Simpan Pembelian</button>
                        </div>
                    </form>
                </div>
                
                <div class="content-section" style="background: #f8f9fa; border-left: 4px solid #667eea;">
                    <h3 style="color: #667eea; margin-bottom: 15px;">ℹ️ Informasi Pembelian</h3>
                    <ul style="margin-left: 20px; line-height: 1.8;">
                        <li><strong>Bibit Ikan:</strong> Akan menambah stok persediaan di inventory</li>
                        <li><strong>Perlengkapan:</strong> Pakan, obat, vitamin untuk budidaya</li>
                        <li><strong>Peralatan:</strong> Alat-alat yang digunakan untuk operasional</li>
                        <li><strong>Bukti Nota:</strong> Upload foto struk untuk dokumentasi</li>
                    </ul>
                </div>
            </div>
        </div>
        
        <script>
        // Format Rupiah otomatis
        document.getElementById('unitPrice').addEventListener('input', function() {{
            calculateTotal();
        }});
        
        document.getElementById('unitPrice').addEventListener('blur', function() {{
            let val = this.value.replace(/[^0-9]/g, '');
            if (val) {{
                let num = parseInt(val);
                this.value = 'Rp' + num.toLocaleString('id-ID') + ',00';
            }}
            calculateTotal();
        }});
        
        document.getElementById('quantity').addEventListener('input', function() {{
            calculateTotal();
        }});
        
        function calculateTotal() {{
            let qtyInput = document.getElementById('quantity');
            let priceInput = document.getElementById('unitPrice');
            let totalInput = document.getElementById('totalPrice');
            
            let qty = parseFloat(qtyInput.value) || 0;
            let priceStr = priceInput.value.replace(/Rp/g, '').replace(/\\./g, '').replace(',', '.').trim();
            let price = parseFloat(priceStr) || 0;
            
            let total = qty * price;
            
            if (total > 0) {{
                totalInput.value = 'Rp' + total.toLocaleString('id-ID', {{
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2
                }}).replace(',', 'X').replace('.', ',').replace('X', '.');
            }} else {{
                totalInput.value = 'Rp0,00';
            }}
        }}
        </script>
    </body>
    </html>
    """
   
    return html


@app.route('/karyawan/edit-purchase/<int:purchase_id>', methods=['GET', 'POST'])
def karyawan_edit_purchase(purchase_id):
    """Edit pembelian karyawan"""
    if 'username' not in session or session.get('role') != 'karyawan':
        flash('Silakan login terlebih dahulu!', 'error')
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Ambil data pembelian
    try:
        response = supabase.table('purchases').select('*').eq('id', purchase_id).execute()
        if not response.data:
            flash('❌ Pembelian tidak ditemukan!', 'error')
            return redirect(url_for('karyawan_purchase_history'))
        
        purchase = response.data[0]
        
        # Pastikan hanya karyawan yang membuat yang bisa edit
        if purchase.get('employee_username') != username:
            flash('❌ Anda tidak berhak mengedit pembelian ini!', 'error')
            return redirect(url_for('karyawan_purchase_history'))
        
    except Exception as e:
        flash(f'❌ Error: {str(e)}', 'error')
        return redirect(url_for('karyawan_purchase_history'))
    
    if request.method == 'POST':
        try:
            item_type = request.form.get('item_type')
            item_name = request.form.get('item_name')
            quantity = float(request.form.get('quantity'))
            unit_price_str = request.form.get('unit_price')
            unit_price = parse_rupiah(unit_price_str)
            total_amount = quantity * unit_price
            
            # Validasi
            if not item_type or not item_name:
                flash('❌ Jenis item dan nama item harus diisi!', 'error')
                return redirect(url_for('karyawan_edit_purchase', purchase_id=purchase_id))
            
            if quantity <= 0:
                flash('❌ Jumlah harus lebih dari 0!', 'error')
                return redirect(url_for('karyawan_edit_purchase', purchase_id=purchase_id))
            
            if unit_price <= 0:
                flash('❌ Harga satuan harus lebih dari 0!', 'error')
                return redirect(url_for('karyawan_edit_purchase', purchase_id=purchase_id))
            
            # Update pembelian
            supabase.table('purchases').update({
                'item_type': item_type,
                'item_name': item_name,
                'quantity': quantity,
                'unit_price': unit_price,
                'total_amount': total_amount,
            }).eq('id', purchase_id).execute()
            
            # Update jurnal terkait
            date_obj = datetime.fromisoformat(purchase['date'].replace('Z', '+00:00'))
            ref_code = f"BL{date_obj.strftime('%d%m')}{purchase['id']:03d}"
            
            # Hapus jurnal lama
            supabase.table('journal_entries').delete().eq('ref_code', ref_code).execute()
            
            # Buat jurnal baru
            account_mapping = {
                'peralatan': ('1-2200', 'Peralatan'),
                'perlengkapan': ('1-1300', 'Perlengkapan'),
                'bibit': ('1-1200', 'Persedeiaan Ikan Mujair')
            }
            
            account_code, account_name = account_mapping.get(item_type, ('1-1300', 'Perlengkapan'))
            date_str = date_obj.strftime('%Y-%m-%d')
            
            # Debit: Aset/Beban
            create_journal_entry(
                date=date_str,
                account_code=account_code,
                account_name=account_name,
                description=f'Pembelian {item_name}',
                debit_account=total_amount,
                credit_account=0,
                journal_type='GJ',
                ref_code=ref_code
            )
            
            # Credit: Kas
            create_journal_entry(
                date=date_str,
                account_code='1-1000',
                account_name='Kas',
                description=f'Pembelian {item_name}',
                debit_account=0,
                credit_account=total_amount,
                journal_type='GJ',
                ref_code=ref_code
            )
            
            flash('✅ Pembelian berhasil diupdate!', 'success')
            return redirect(url_for('karyawan_purchase_history'))
            
        except Exception as e:
            flash(f'❌ Error: {str(e)}', 'error')
            return redirect(url_for('karyawan_edit_purchase', purchase_id=purchase_id))
    
    # Generate HTML form edit
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Edit Pembelian - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">👷</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Karyawan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/karyawan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/karyawan/purchase"><span class="icon">🛒</span> Pembelian</a></li>
                    <li><a href="/karyawan/purchase-history" class="active"><span class="icon">📋</span> Riwayat</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Edit Pembelian</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <h2>✏️ Edit Data Pembelian</h2>
                    {flash_html}
                    <form method="POST">
                        <div class="form-row">
                            <div class="form-group">
                                <label>Jenis Item *</label>
                                <select name="item_type" required id="itemType">
                                    <option value="">-- Pilih Jenis --</option>
                                    <option value="bibit" {'selected' if purchase['item_type'] == 'bibit' else ''}>🐟 Bibit Ikan Mujair</option>
                                    <option value="perlengkapan" {'selected' if purchase['item_type'] == 'perlengkapan' else ''}>📦 Perlengkapan</option>
                                    <option value="peralatan" {'selected' if purchase['item_type'] == 'peralatan' else ''}>🔧 Peralatan</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Nama Item *</label>
                                <input type="text" name="item_name" required value="{purchase['item_name']}" placeholder="Contoh: Pakan Ikan">
                            </div>
                        </div>
                        
                        <div class="form-row">
                            <div class="form-group">
                                <label>Jumlah/Kuantitas *</label>
                                <input type="number" name="quantity" step="0.01" min="0.01" required value="{purchase['quantity']}" id="quantity">
                            </div>
                            <div class="form-group">
                                <label>Harga Satuan *</label>
                                <input type="text" name="unit_price" required value="{format_rupiah(purchase['unit_price'])}" id="unitPrice">
                            </div>
                            <div class="form-group">
                                <label>Total Harga</label>
                                <input type="text" id="totalPrice" readonly placeholder="Rp0,00" style="background: #f0f0f0;" value="{format_rupiah(purchase['total_amount'])}">
                            </div>
                        </div>
                        
                        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-top: 20px;">
                            <a href="/karyawan/purchase-history" class="btn-sm btn-secondary btn-block">↩️ Batal</a>
                            <button type="submit" class="btn-sm btn-success btn-block">💾 Simpan Perubahan</button>
                        </div>
                    </form>
                </div>
            </div>
        </div>
        
        <script>
        // Format Rupiah otomatis
        document.getElementById('unitPrice').addEventListener('input', function() {{
            calculateTotal();
        }});
        
        document.getElementById('unitPrice').addEventListener('blur', function() {{
            let val = this.value.replace(/[^0-9]/g, '');
            if (val) {{
                let num = parseInt(val);
                this.value = 'Rp' + num.toLocaleString('id-ID') + ',00';
            }}
            calculateTotal();
        }});
        
        document.getElementById('unitPrice').addEventListener('focus', function() {{
            let val = this.value.replace(/[^0-9]/g, '');
            if (val) {{
                this.value = val;
            }}
        }});
        
        document.getElementById('quantity').addEventListener('input', function() {{
            calculateTotal();
        }});
        
        function calculateTotal() {{
            let qtyInput = document.getElementById('quantity');
            let priceInput = document.getElementById('unitPrice');
            let totalInput = document.getElementById('totalPrice');
            
            let qty = parseFloat(qtyInput.value) || 0;
            let priceStr = priceInput.value.replace(/Rp/g, '').replace(/\\./g, '').replace(',', '.').trim();
            let price = parseFloat(priceStr) || 0;
            
            let total = qty * price;
            
            if (total > 0) {{
                totalInput.value = 'Rp' + total.toLocaleString('id-ID', {{
                    minimumFractionDigits: 2,
                    maximumFractionDigits: 2
                }}).replace(',', 'X').replace('.', ',').replace('X', '.');
            }} else {{
                totalInput.value = 'Rp0,00';
            }}
        }}
        
        // Update datetime
        function updateDateTime() {{
            const now = new Date();
            const options = {{ 
                weekday: 'long', 
                year: 'numeric', 
                month: 'long', 
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            }};
            const elem = document.getElementById('datetime');
            if (elem) elem.textContent = now.toLocaleDateString('id-ID', options);
        }}
        setInterval(updateDateTime, 1000);
        updateDateTime();
        </script>
    </body>
    </html>
    """
    
    return html

@app.route('/karyawan/delete-purchase/<int:purchase_id>', methods=['GET'])
def karyawan_delete_purchase(purchase_id):
    """Hapus pembelian karyawan"""
    if 'username' not in session or session.get('role') != 'karyawan':
        flash('❌ Silakan login terlebih dahulu!', 'error')
        return redirect(url_for('login'))
    
    try:
        username = session.get('username')
        
        # Cek pembelian
        response = supabase.table('purchases').select('*').eq('id', purchase_id).execute()
        
        if not response.data:
            flash('❌ Pembelian tidak ditemukan', 'error')
            return redirect(url_for('karyawan_purchase_history'))
        
        purchase = response.data[0]
        
        # Pastikan hanya karyawan yang membuat yang bisa hapus
        if purchase.get('employee_username') != username:
            flash('❌ Tidak berhak menghapus pembelian ini', 'error')
            return redirect(url_for('karyawan_purchase_history'))
        
        # Hapus jurnal terkait terlebih dahulu
        date_obj = datetime.fromisoformat(purchase['date'].replace('Z', '+00:00'))
        ref_code = f"BL{date_obj.strftime('%d%m')}{purchase['id']:03d}"
        
        print(f"🔍 Menghapus jurnal dengan ref_code: {ref_code}")  # Debug
        
        # Hapus semua jurnal dengan ref_code ini
        journal_response = supabase.table('journal_entries').delete().eq('ref_code', ref_code).execute()
        print(f"✅ Jurnal dihapus: {journal_response}")  # Debug
        
        # Hapus pembelian
        purchase_response = supabase.table('purchases').delete().eq('id', purchase_id).execute()
        print(f"✅ Pembelian dihapus: {purchase_response}")  # Debug
        
        flash('✅ Pembelian berhasil dihapus', 'success')
        
    except Exception as e:
        print(f"❌ ERROR DELETE: {str(e)}")  # Debug
        import traceback
        traceback.print_exc()
        flash(f'❌ Error: {str(e)}', 'error')
    
    return redirect(url_for('karyawan_purchase_history'))

@app.route('/karyawan/purchase-history')
def karyawan_purchase_history():
    """Riwayat pembelian karyawan"""
    if 'username' not in session or session.get('role') != 'karyawan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Filter berdasarkan username
    all_purchases = get_purchases()
    purchases = [p for p in all_purchases if p.get('employee_username') == username]
    
    # Flash messages
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    purchases_html = ""
    for p in purchases:
        date_obj = datetime.fromisoformat(p['date'].replace('Z', '+00:00'))
        ref_code = f"BL{date_obj.strftime('%d%m')}{p['id']:03d}"
        
        # Escape untuk JavaScript
        item_name_safe = p['item_name'].replace("'", "\\'").replace('"', '\\"')
        
        purchases_html += f"""
        <tr>
            <td class="text-center">{ref_code}</td>
            <td>{date_obj.strftime('%d/%m/%Y %H:%M')}</td>
            <td style="text-transform: capitalize;">
                {'🐟 ' if p['item_type'] == 'bibit' else '📦 ' if p['item_type'] == 'perlengkapan' else '🔧 '}
                {p['item_type']}
            </td>
            <td>{p['item_name']}</td>
            <td class="text-right">{p['quantity']}</td>
            <td class="text-right">{format_rupiah(p['unit_price'])}</td>
            <td class="text-right"><strong>{format_rupiah(p['total_amount'])}</strong></td>
            <td class="text-center">
                <div class="btn-group">
                    <a href="/karyawan/edit-purchase/{p['id']}" class="btn-sm btn-warning" title="Edit Pembelian">
                        ✏️ Edit
                    </a>
                    <a href="/karyawan/delete-purchase/{p['id']}" 
                       class="btn-sm btn-danger" 
                       title="Hapus Pembelian"
                       onclick="return confirm('🗑️ Yakin ingin menghapus pembelian:\\n\\n{item_name_safe}?');">
                        🗑️ Hapus
                    </a>
                </div>
            </td>
        </tr>
        """
    
    total_pembelian = sum(float(p['total_amount']) for p in purchases)
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Riwayat Pembelian - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">👷</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Karyawan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/karyawan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/karyawan/purchase"><span class="icon">🛒</span> Pembelian</a></li>
                    <li><a href="/karyawan/purchase-history" class="active"><span class="icon">📋</span> Riwayat</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Riwayat Pembelian</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                {flash_html}
                
                <div class="stats-grid" style="grid-template-columns: repeat(2, 1fr);">
                    <div class="stat-card">
                        <div class="stat-icon">📦</div>
                        <div class="stat-value">{len(purchases)}</div>
                        <div class="stat-label">Total Pembelian</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">💰</div>
                        <div class="stat-value">{format_rupiah(total_pembelian)}</div>
                        <div class="stat-label">Total Pengeluaran</div>
                    </div>
                </div>
                
                <div class="content-section">
                    <h2>📋 Daftar Pembelian</h2>
                    <a href="/karyawan/purchase" class="btn-sm btn-success" style="margin-bottom: 20px;">➕ Pembelian Baru</a>
                    
                    <div style="overflow-x: auto;">
                        <table>
                            <thead>
                                <tr>
                                    <th class="text-center">Kode</th>
                                    <th>Tanggal</th>
                                    <th>Jenis</th>
                                    <th>Item</th>
                                    <th class="text-right">Qty</th>
                                    <th class="text-right">Harga Satuan</th>
                                    <th class="text-right">Total</th>
                                    <th class="text-center">Aksi</th>
                                </tr>
                            </thead>
                            <tbody>
                                {purchases_html if purchases_html else '<tr><td colspan="8" class="text-center">Belum ada pembelian</td></tr>'}
                            </tbody>
                            {f"""
                            <tfoot style="background: #f8f9fa; font-weight: bold;">
                                <tr>
                                    <td colspan="6" class="text-right" style="padding: 15px;">TOTAL KESELURUHAN:</td>
                                    <td class="text-right" style="padding: 15px; color: #667eea; font-size: 18px;">{format_rupiah(total_pembelian)}</td>
                                    <td></td>
                                </tr>
                            </tfoot>
                            """ if purchases else ''}
                        </table>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
        // Update datetime
        function updateDateTime() {{
            const now = new Date();
            const options = {{ 
                weekday: 'long', 
                year: 'numeric', 
                month: 'long', 
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            }};
            const elem = document.getElementById('datetime');
            if (elem) elem.textContent = now.toLocaleDateString('id-ID', options);
        }}
        setInterval(updateDateTime, 1000);
        updateDateTime();
        </script>
    </body>
    </html>
    """
    return html

@app.route('/karyawan/pembelian/submit', methods=['POST'])
def karyawan_submit_pembelian():
    """Submit pembelian dan auto-create inventory entry"""
    if 'username' not in session or session.get('role') != 'karyawan':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    try:
        data = request.get_json()
        
        # Data pembelian
        date = data.get('date')
        supplier = data.get('supplier')
        quantity = float(data.get('quantity', 0))
        unit_price = float(data.get('unit_price', 0))
        total_amount = quantity * unit_price
        description = data.get('description', f'Pembelian dari {supplier}')
        username = session.get('username')
        
        # 1. Insert ke tabel pembelian (sesuaikan dengan tabel pembelian kamu)
        purchase = supabase.table('purchases').insert({
            'date': date,
            'supplier': supplier,
            'quantity': quantity,
            'unit_price': unit_price,
            'total_amount': total_amount,
            'description': description,
            'employee': username,
            'status': 'pending'  # Menunggu approval akuntan
        }).execute()
        
        if not purchase.data:
            return jsonify({'success': False, 'message': 'Gagal insert pembelian'}), 500
        
        purchase_id = purchase.data[0]['id']
        ref_code = f"PB{purchase_id:04d}"
        
        # 2. Auto-create inventory entry (barang masuk)
        inventory_entry = create_inventory_entry(
            date=date,
            ref_code=ref_code,
            description=description,
            quantity_in=quantity,
            quantity_out=0,
            unit_price=unit_price,
            employee=username
        )
        
        if not inventory_entry:
            # Rollback pembelian jika inventory gagal
            supabase.table('purchases').delete().eq('id', purchase_id).execute()
            return jsonify({'success': False, 'message': 'Gagal create inventory entry'}), 500
        
        # 3. (OPSIONAL) Create jurnal pembelian
        # create_journal_entry_pembelian(purchase_id, date, total_amount)
        
        return jsonify({
            'success': True, 
            'message': 'Pembelian berhasil disubmit!',
            'purchase_id': purchase_id,
            'inventory_id': inventory_entry['id']
        }), 200
        
    except Exception as e:
        print(f"❌ Error submit pembelian: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/akuntan/accounts', methods=['GET', 'POST'])
def akuntan_accounts():
    """Kelola daftar akun"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            account_code = request.form.get('account_code', '').strip()
            account_name = request.form.get('account_name', '').strip()
            account_type = request.form.get('account_type', '').strip()
            normal_balance = request.form.get('normal_balance', '').strip()
            beginning_balance_str = request.form.get('beginning_balance', '0')
            
            # ✅ VALIDASI INPUT
            if not account_code or not account_name or not account_type or not normal_balance:
                flash('Semua field harus diisi!', 'error')
                return redirect(url_for('akuntan_accounts'))
            
            # ✅ VALIDASI FORMAT KODE AKUN
            import re
            if not re.match(r'^[0-9]-[0-9]{4}$', account_code):
                flash('Format kode akun salah! Gunakan format X-XXXX (contoh: 1-1000)', 'error')
                return redirect(url_for('akuntan_accounts'))
            
            # ✅ CEK DUPLIKASI KODE AKUN
            existing = supabase.table('accounts').select('account_code').eq('account_code', account_code).execute()
            if existing.data:
                flash(f'Kode akun {account_code} sudah ada!', 'error')
                return redirect(url_for('akuntan_accounts'))
            
            # ✅ PARSE BEGINNING BALANCE
            beginning_balance = parse_rupiah(beginning_balance_str)
            
            # ✅ SIMPAN AKUN
            result = create_account(account_code, account_name, account_type, normal_balance, beginning_balance)
            
            if result:
                flash(f'✅ Akun {account_code} - {account_name} berhasil ditambahkan!', 'success')
            else:
                flash('❌ Gagal menambahkan akun!', 'error')
                
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
        
        return redirect(url_for('akuntan_accounts'))
    
    # ============= GET METHOD =============
    username = session.get('username', 'User')
    accounts = get_all_accounts()
    
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    # Generate tabel akun
    accounts_html = ""
    for acc in accounts:
        balance = get_ledger_balance(acc['account_code'])
        
        # Escape untuk JavaScript - penting untuk modal
        account_json = {
            'account_code': acc['account_code'],
            'account_name': acc['account_name'],
            'normal_balance': acc['normal_balance'],
            'beginning_balance': acc.get('beginning_balance', 0)
        }
        import json
        account_data = json.dumps(account_json).replace('"', '&quot;')
        
        accounts_html += f"""
        <tr>
            <td class="text-center"><strong>{acc['account_code']}</strong></td>
            <td>{acc['account_name']}</td>
            <td class="text-center" style="text-transform: capitalize;">{acc['normal_balance']}</td>
            <td class="text-right">{format_rupiah(acc.get('beginning_balance', 0))}</td>
            <td class="text-right"><strong>{format_rupiah(balance)}</strong></td>
            <td class="text-center">
                <div class="btn-group">
                    <button class="btn-sm btn-warning" onclick='showEditModal({account_data})' title="Edit Akun">✏️ Edit</button>
                    <button class="btn-sm btn-danger" onclick="deleteAccount('{acc['account_code']}')" title="Hapus Akun">🗑️ Hapus</button>
                </div>
            </td>
        </tr>
        """
    
    # ============= HTML LENGKAP =============
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daftar Akun - Geboy Mujair</title>
        {generate_dashboard_style()}
        <style>
            .btn-group {{
                display: flex;
                gap: 5px;
                justify-content: center;
                flex-wrap: wrap;
            }}
            .btn-group .btn-sm {{
                padding: 6px 12px;
                font-size: 13px;
            }}
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            <!-- ============= SIDEBAR ============= -->
            {generate_sidebar('akuntan', username, 'accounts')}
            
            <!-- ============= MAIN CONTENT ============= -->
            <div class="main-content">
                <div class="top-bar">
                    <h1>Daftar Akun (Chart of Accounts)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <!-- ============= SECTION RESET ============= -->
                <div class="content-section" style="background: #fff3cd; border-left: 4px solid #ffc107;">
                    <h3 style="color: #856404; margin-bottom: 15px;">⚙️ Pengaturan Chart of Accounts</h3>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px;">
                        <button onclick="resetAccounts('clear')" class="btn-sm btn-danger btn-block">
                            🗑️ Hapus Semua Akun
                        </button>
                        <button onclick="resetAccounts('default')" class="btn-sm btn-warning btn-block">
                            🔄 Reset ke Default
                        </button>
                    </div>
                    <small style="color: #856404; display: block; margin-top: 10px;">
                        ⚠️ <strong>Peringatan:</strong> Reset akan menghapus SEMUA jurnal entries terkait!
                    </small>
                </div>
                
                <!-- ============= FORM TAMBAH AKUN ============= -->
                <div class="content-section">
                    <h2>➕ Tambah Akun Baru</h2>
                    {flash_html}
                    <form method="POST">
                        <div class="form-row">
                            <div class="form-group">
                                <label>Kode Akun *</label>
                                <input type="text" name="account_code" required placeholder="1-1000" 
                                       pattern="[0-9]-[0-9]{{4}}"
                                       title="Format: X-XXXX (contoh: 1-1101)"
                                       maxlength="6">
                                <small style="color: #666;">Format: X-XXXX (contoh: 1-1101)</small>
                            </div>
                            <div class="form-group">
                                <label>Nama Akun *</label>
                                <input type="text" name="account_name" required placeholder="Kas">
                            </div>
                        </div>
                        
                        <div class="form-row">
                            <div class="form-group">
                                <label>Tipe Akun *</label>
                                <select name="account_type" required>
                                    <option value="">-- Pilih Tipe --</option>
                                    <option value="aset">Aset (1-xxxx)</option>
                                    <option value="kewajiban">Kewajiban (2-xxxx)</option>
                                    <option value="ekuitas">Ekuitas (3-xxxx)</option>
                                    <option value="pendapatan">Pendapatan (4-xxxx)</option>
                                    <option value="beban">Beban (5-xxxx, 6-xxxx)</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Saldo Normal *</label>
                                <select name="normal_balance" required>
                                    <option value="">-- Pilih --</option>
                                    <option value="debit">Debit</option>
                                    <option value="credit">Kredit</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Saldo Awal</label>
                                <input type="text" name="beginning_balance" placeholder="Rp0,00" class="rupiah-input">
                            </div>
                        </div>
                        
                        <button type="submit" class="btn-sm btn-success btn-block">💾 Tambah Akun</button>
                    </form>
                </div>
                
                <!-- ============= TABEL CHART OF ACCOUNTS ============= -->
                <div class="content-section">
                    <h2>📋 Chart of Accounts</h2>
                    <div style="overflow-x: auto;">
                        <table>
                            <thead>
                                <tr>
                                    <th class="text-center">Kode</th>
                                    <th>Nama Akun</th>
                                    <th class="text-center">Saldo Normal</th>
                                    <th class="text-right">Saldo Awal</th>
                                    <th class="text-right">Saldo Saat Ini</th>
                                    <th class="text-center">Aksi</th>
                                </tr>
                            </thead>
                            <tbody>
                                {accounts_html if accounts_html else '<tr><td colspan="6" class="text-center">Belum ada akun</td></tr>'}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- ============= MODAL EDIT AKUN ============= -->
        <div id="editModal" class="modal">
            <div class="modal-content">
                <span class="close" onclick="closeModal('editModal')">&times;</span>
                <h2>✏️ Edit Akun</h2>
                <form method="POST" action="/akuntan/accounts/edit" id="editForm">
                    <input type="hidden" name="account_code" id="edit_account_code">
                    
                    <div class="form-group">
                        <label>Kode Akun</label>
                        <input type="text" id="display_account_code" readonly style="background: #f0f0f0; cursor: not-allowed;">
                        <small style="color: #666;">Kode akun tidak dapat diubah</small>
                    </div>
                    
                    <div class="form-group">
                        <label>Nama Akun *</label>
                        <input type="text" name="account_name" id="edit_account_name" required placeholder="Masukkan nama akun">
                    </div>
                    
                    <div class="form-group">
                        <label>Saldo Normal</label>
                        <input type="text" id="display_normal_balance" readonly style="background: #f0f0f0; cursor: not-allowed; text-transform: capitalize;">
                        <small style="color: #666;">Saldo normal tidak dapat diubah</small>
                    </div>
                    
                    <div class="form-group">
                        <label>Saldo Awal</label>
                        <input type="text" name="beginning_balance" id="edit_beginning_balance" placeholder="Rp0,00" class="rupiah-input-edit">
                    </div>
                    
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px; margin-top: 20px;">
                        <button type="button" class="btn-sm btn-secondary btn-block" onclick="closeModal('editModal')">↩️ Batal</button>
                        <button type="submit" class="btn-sm btn-warning btn-block">💾 Simpan Perubahan</button>
                    </div>
                </form>
            </div>
        </div>
        
        <!-- ============= JAVASCRIPT ============= -->
        <script>
        // Format rupiah otomatis untuk input tambah akun
        document.querySelectorAll('.rupiah-input').forEach(input => {{
            input.addEventListener('blur', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = 'Rp' + parseInt(val).toLocaleString('id-ID') + ',00';
                }}
            }});
            
            input.addEventListener('focus', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = val;
                }}
            }});
        }});
        
        // Format rupiah untuk modal edit
        document.querySelectorAll('.rupiah-input-edit').forEach(input => {{
            input.addEventListener('blur', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = 'Rp' + parseInt(val).toLocaleString('id-ID') + ',00';
                }}
            }});
            
            input.addEventListener('focus', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = val;
                }}
            }});
        }});
        
        // Show edit modal
        function showEditModal(accountData) {{
            document.getElementById('edit_account_code').value = accountData.account_code;
            document.getElementById('display_account_code').value = accountData.account_code;
            document.getElementById('edit_account_name').value = accountData.account_name;
            document.getElementById('display_normal_balance').value = accountData.normal_balance;
            document.getElementById('edit_beginning_balance').value = 'Rp' + parseInt(accountData.beginning_balance).toLocaleString('id-ID') + ',00';
            document.getElementById('editModal').style.display = 'block';
        }}
        
        // Delete akun dengan konfirmasi
        function deleteAccount(code) {{
            if (!confirm('⚠️ PERINGATAN!\\n\\nMenghapus akun ' + code + ' akan menghapus SEMUA jurnal entry yang menggunakan akun ini.\\n\\nYakin ingin melanjutkan?')) {{
                return;
            }}
            
            fetch('/akuntan/accounts/delete/' + encodeURIComponent(code), {{
                method: 'DELETE'
            }})
            .then(res => res.json())
            .then(data => {{
                if (data.success) {{
                    alert('✅ ' + data.message);
                    location.reload();
                }} else {{
                    alert('❌ Error: ' + data.message);
                }}
            }})
            .catch(err => {{
                console.error(err);
                alert('❌ Terjadi error saat menghapus akun');
            }});
        }}
        
        // Reset Chart of Accounts
        function resetAccounts(resetType) {{
            const messages = {{
                'clear': 'menghapus SEMUA akun dan jurnal.\\n\\nAnda harus input ulang dari awal!',
                'default': 'mengembalikan ke akun default.\\n\\nSemua akun custom dan jurnal akan dihapus!'
            }};
            
            if (!confirm('⚠️⚠️ PERINGATAN BESAR! ⚠️⚠️\\n\\nTindakan ini akan ' + messages[resetType] + '\\n\\nYAKIN 100% ingin melanjutkan?')) {{
                return;
            }}
            
            if (!confirm('Konfirmasi terakhir: Anda yakin?')) {{
                return;
            }}
            
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = '/akuntan/accounts/reset';
            
            const input = document.createElement('input');
            input.type = 'hidden';
            input.name = 'reset_type';
            input.value = resetType;
            
            form.appendChild(input);
            document.body.appendChild(form);
            form.submit();
        }}
        
        // Close modal
        function closeModal(modalId) {{
            document.getElementById(modalId).style.display = 'none';
        }}
        
        // Close modal when clicking outside
        window.onclick = function(event) {{
            if (event.target.className === 'modal') {{
                event.target.style.display = 'none';
            }}
        }}
        
        // Update datetime
        function updateDateTime() {{
            const now = new Date();
            const options = {{ 
                weekday: 'long', 
                year: 'numeric', 
                month: 'long', 
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            }};
            const elem = document.getElementById('datetime');
            if (elem) elem.textContent = now.toLocaleDateString('id-ID', options);
        }}
        setInterval(updateDateTime, 1000);
        updateDateTime();
        </script>
    </body>
    </html>
    """
    
    return html

@app.route('/akuntan/accounts/reset', methods=['POST'])
def akuntan_accounts_reset():
    """Reset Chart of Accounts ke default atau kosong"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        reset_type = request.form.get('reset_type', 'clear')  # 'clear' atau 'default'
        
        # ✅ HAPUS SEMUA JURNAL ENTRIES DULU
        supabase.table('journal_entries').delete().neq('id', 0).execute()  # Hapus semua
        
        # ✅ HAPUS SEMUA AKUN
        supabase.table('accounts').delete().neq('account_code', '').execute()  # Hapus semua
        
        if reset_type == 'default':
            # ✅ RE-INITIALIZE DEFAULT ACCOUNTS
            init_default_accounts()
            flash('Chart of Accounts berhasil direset ke pengaturan default!', 'success')
        else:
            flash('Semua akun berhasil dihapus! Silakan input akun baru.', 'success')
        
        return redirect(url_for('akuntan_accounts'))
        
    except Exception as e:
        flash(f'Error reset: {str(e)}', 'error')
        return redirect(url_for('akuntan_accounts'))

@app.route('/akuntan/accounts/edit', methods=['POST'])
def akuntan_edit_account_new():
    """Edit akun dengan modal"""
    if 'username' not in session or session.get('role') != 'akuntan':
        flash('❌ Unauthorized', 'error')
        return redirect(url_for('login'))
    
    try:
        account_code = request.form.get('account_code', '').strip()
        account_name = request.form.get('account_name', '').strip()
        beginning_balance_str = request.form.get('beginning_balance', '0')
        
        if not account_name:
            flash('❌ Nama akun tidak boleh kosong!', 'error')
            return redirect(url_for('akuntan_accounts'))
        
        # ✅ PARSE BEGINNING BALANCE (handle format Rupiah)
        beginning_balance = parse_rupiah(beginning_balance_str)
        
        # ✅ VALIDASI NILAI BALANCE (max 10 triliun untuk numeric(15,2))
        MAX_BALANCE = 9999999999999.99
        if abs(beginning_balance) > MAX_BALANCE:
            flash(f'❌ Saldo terlalu besar! Maksimal {format_rupiah(MAX_BALANCE)}', 'error')
            return redirect(url_for('akuntan_accounts'))
        
        # ✅ CEK APAKAH AKUN ADA
        check_account = supabase.table('accounts').select('*').eq('account_code', account_code).execute()
        
        if not check_account.data:
            flash(f'❌ Akun {account_code} tidak ditemukan!', 'error')
            return redirect(url_for('akuntan_accounts'))
        
        # ✅ UPDATE AKUN
        update_data = {
            'account_name': account_name,
            'beginning_balance': round(float(beginning_balance), 2)
        }
        
        response = supabase.table('accounts').update(update_data).eq('account_code', account_code).execute()
        
        # ✅ VALIDASI RESPONSE
        if response.data and len(response.data) > 0:
            flash(f'✅ Akun {account_code} - {account_name} berhasil diupdate!', 'success')
        else:
            flash('❌ Gagal update akun! Data tidak berubah atau terjadi error.', 'error')
            
    except Exception as e:
        flash(f'❌ Error: {str(e)}', 'error')
        print(f"ERROR UPDATE ACCOUNT: {e}")
        import traceback
        traceback.print_exc()
    
    return redirect(url_for('akuntan_accounts'))

@app.route('/akuntan/accounts/delete/<account_code>', methods=['DELETE'])
def akuntan_delete_account(account_code):
    if 'username' not in session or session.get('role') != 'akuntan':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        # ✅ CEK DULU ADA JURNAL TERKAIT ATAU TIDAK
        all_journals = supabase.table('journal_entries').select('id').eq('account_code', account_code).execute()
        
        journal_count = len(all_journals.data) if all_journals.data else 0
        
        if journal_count > 0:
            # ✅ HAPUS SEMUA JURNAL ENTRIES DULU (CASCADE DELETE)
            supabase.table('journal_entries').delete().eq('account_code', account_code).execute()
        
        # ✅ BARU HAPUS AKUN
        supabase.table('accounts').delete().eq('account_code', account_code).execute()
        
        return jsonify({
            'success': True, 
            'message': f'Akun {account_code} berhasil dihapus (termasuk {journal_count} jurnal entry terkait)'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/akuntan/manual-transaction', methods=['GET', 'POST'])
def akuntan_manual_transaction():
    """Akuntan input transaksi manual - PERPETUAL METHOD (VERSI PERBAIKAN FINAL)"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    # ====================================================================
    # ======================== BLOK UNTUK METHOD POST ====================
    # ====================================================================
    if request.method == 'POST':
        # Hanya ada satu blok try...except untuk membungkus semua logika
        try:
            transaction_type = request.form.get('transaction_type')
            date = request.form.get('date')
            amount = parse_rupiah(request.form.get('amount'))
            description = request.form.get('description')

            # MAPPING TRANSAKSI (Ini sudah benar)
            transaction_mapping = {
                'pembelian_bibit': {
                    'entries': [
                        {'account_code': '1-1200', 'account_name': 'Persediaan Ikan Mujair', 'debit': amount, 'credit': 0},
                        {'account_code': '1-1000', 'account_name': 'Kas', 'debit': 0, 'credit': amount}
                    ], 'desc': 'Pembelian bibit ikan mujair', 'icon': '🐟'
                },
                'pembelian_perlengkapan': {
                    'entries': [
                        {'account_code': '1-1300', 'account_name': 'Perlengkapan', 'debit': amount, 'credit': 0},
                        {'account_code': '1-1000', 'account_name': 'Kas', 'debit': 0, 'credit': amount}
                    ], 'desc': 'Pembelian perlengkapan budidaya', 'icon': '📦'
                },
                'pembelian_peralatan': {
                    'entries': [
                        {'account_code': '1-2200', 'account_name': 'Peralatan', 'debit': amount, 'credit': 0},
                        {'account_code': '1-1000', 'account_name': 'Kas', 'debit': 0, 'credit': amount}
                    ], 'desc': 'Pembelian peralatan budidaya', 'icon': '🔧'
                },
                'pembayaran_gaji': {
                    'entries': [
                        {'account_code': '6-1300', 'account_name': 'Beban Gaji', 'debit': amount, 'credit': 0},
                        {'account_code': '1-1000', 'account_name': 'Kas', 'debit': 0, 'credit': amount}
                    ], 'desc': 'Pembayaran gaji karyawan', 'icon': '👨‍💼'
                },
                'pembayaran_listrik': {
                    'entries': [
                        {'account_code': '6-1000', 'account_name': 'Beban Listrik', 'debit': amount, 'credit': 0},
                        {'account_code': '1-1000', 'account_name': 'Kas', 'debit': 0, 'credit': amount}
                    ], 'desc': 'Pembayaran listrik', 'icon': '⚡'
                },
                'penerimaan_kas': {
                    'entries': [
                        {'account_code': '1-1000', 'account_name': 'Kas', 'debit': amount, 'credit': 0},
                        {'account_code': '4-1201', 'account_name': 'Pendapatan Lain-lain', 'debit': 0, 'credit': amount}
                    ], 'desc': 'Penerimaan kas lain-lain', 'icon': '💰'
                }
            }

            mapping = transaction_mapping.get(transaction_type)
            if not mapping:
                flash('❌ Tipe transaksi tidak valid!', 'error')
                return redirect(url_for('akuntan_manual_transaction'))

            if amount <= 0:
                flash('❌ Jumlah harus lebih dari 0!', 'error')
                return redirect(url_for('akuntan_manual_transaction'))

            ref_code = f"MT-{datetime.now().strftime('%d%m%Y-%H%M%S')}"
            final_desc = description or mapping['desc']

            # 1. Kumpulkan semua data entri ke dalam satu list
            journal_entries_to_insert = []
            for entry in mapping['entries']:
                journal_entries_to_insert.append({
                    'date': date,
                    'account_code': entry['account_code'],
                    'account_name': entry['account_name'],
                    'description': final_desc,
                    'debit': float(entry['debit']),
                    'credit': float(entry['credit']),
                    'journal_type': 'GJ',
                    'ref_code': ref_code
                })
            
            # 2. Kirim semua data dalam satu perintah INSERT (Bulk Insert)
            print(f"🔍 DEBUG: Mengirim {len(journal_entries_to_insert)} entri ke database untuk Ref: {ref_code}...")
            response = supabase.table('journal_entries').insert(journal_entries_to_insert).execute()

            # 3. Periksa hasilnya
            if response.data:
                print(f"   ✅ {len(response.data)} entri jurnal berhasil disimpan.")
                bibit_quantity = None
                bibit_price_per_kg = None
                # 4. Lanjutkan ke logika inventory HANYA JIKA JURNAL BERHASIL
                if transaction_type == 'pembelian_bibit':
                    bibit_quantity = request.form.get('bibit_quantity')
                    bibit_price_per_kg = request.form.get('bibit_price_per_kg')
                    print("   -> Mencatat ke kartu inventaris...")
                    # Validasi input bibit
                    if not bibit_quantity or not bibit_price_per_kg:
                        flash('❌ Kuantitas dan Harga Bibit harus diisi!', 'error')
                        return redirect(url_for('akuntan_manual_transaction'))
                    try:
                        bibit_quantity = float(bibit_quantity)
                        bibit_price_per_kg = parse_rupiah(bibit_price_per_kg) # Gunakan fungsi parse_rupiah Anda
                        if bibit_quantity <= 0 or bibit_price_per_kg <= 0:
                            flash('❌ Kuantitas dan Harga Bibit harus lebih dari 0!', 'error')
                            return redirect(url_for('akuntan_manual_transaction'))
                        
                        # ✅ Hitung ulang 'amount' berdasarkan input bibit
                        amount = bibit_quantity * bibit_price_per_kg  # Update nilai 'amount'
                    
                    except ValueError:
                        flash('❌ Format Kuantitas atau Harga Bibit tidak valid!', 'error')
                        return redirect(url_for('akuntan_manual_transaction'))
                    
                    # Ambil saldo terakhir dengan lebih aman
                    last_qty = 0
                    last_balance_amount = 0
                    # Hanya ambil kolom yang dibutuhkan
                    last_entry_response = supabase.table('inventory_card')\
                        .select('balance_quantity, balance_amount')\
                        .eq('product_name', 'Ikan Mujair')\
                        .order('id', desc=True)\
                        .limit(1)\
                        .execute()
                    
                    if last_entry_response.data:
                        # Ambil data pertama dengan aman menggunakan .get()
                        last_data = last_entry_response.data[0]
                        last_qty = last_data.get('balance_quantity', 0)
                        last_balance_amount = last_data.get('balance_amount', 0)
                    
                    # Estimasi quantity
                    unit_price = 30000 # Pastikan ini sesuai dengan harga per unit bibit Anda
                    estimated_qty = amount / unit_price
                    new_balance_qty = last_qty + estimated_qty
                    new_balance_amount = last_balance_amount + amount
                    
                    # ✅ INSERT KE INVENTORY CARD
                    supabase.table('inventory_card').insert({
                        'date': date,
                        'doc_no': ref_code,
                        'description': final_desc,
                        'product_name': 'Ikan Mujair',
                        'purchase_quantity': bibit_quantity,  # Gunakan kuantitas yang diinput
                        'purchase_unit_price': bibit_price_per_kg,  # Gunakan harga per kg yang diinput
                        'purchase_amount': amount,  # Gunakan 'amount' yang sudah dihitung ulang
                        'sales_quantity': 0,
                        'sales_unit_price': 0,
                        'sales_amount': 0,
                        'balance_quantity': new_balance_qty,
                        'balance_unit_price': bibit_price_per_kg, # Harga per kg jadi harga saldo
                        'balance_amount': new_balance_amount,
                        'employee': session.get('username')
                    }).execute()
                    print("   -> ✅ Kartu inventaris berhasil dicatat.")

                flash(f'✅ {mapping["icon"]} {mapping["desc"]} berhasil dicatat! (Ref: {ref_code})', 'success') 
            else:
                # Jika tidak ada data yang kembali, anggap sebagai error 
                flash('❌ Gagal menyimpan entri jurnal! Tidak ada data yang dikembalikan oleh server.', 'error')
        
        # Ini adalah blok 'except' untuk 'try' di atas
        except Exception as e:
            flash(f'❌ Terjadi Error: {str(e)}', 'error')
            import traceback
            traceback.print_exc() # Mencetak error detail di konsol untuk debug
        
        # Redirect selalu dilakukan di akhir, di luar try-except
        return redirect(url_for('akuntan_manual_transaction'))
    
    # ===================================================================
    # ======================== BLOK UNTUK METHOD GET ====================
    # ===================================================================
    # (Bagian ini tidak diubah, karena sudah benar)
    username = session.get('username', 'User')
    all_journals = get_journal_entries(journal_type='GJ')
    
    grouped = {}
    for j in all_journals:
        ref = j.get('ref_code', '')
        if ref.startswith('MT-'):
            if ref not in grouped:
                grouped[ref] = {
                    'date': j['date'], 'description': j['description'], 
                    'debit_entries': [], 'credit_entries': [],
                    'total_debit': 0, 'total_credit': 0
                }
            if j.get('debit', 0) > 0:
                grouped[ref]['debit_entries'].append({'account': f"{j['account_code']} - {j['account_name']}", 'amount': j['debit']})
                grouped[ref]['total_debit'] += j['debit']
            if j.get('credit', 0) > 0:
                grouped[ref]['credit_entries'].append({'account': f"{j['account_code']} - {j['account_name']}", 'amount': j['credit']})
                grouped[ref]['total_credit'] += j['credit']
    
    manual_transactions = [{'ref_code': k, **v} for k, v in grouped.items()]
    manual_transactions.sort(key=lambda x: x['date'], reverse=True)
    
    flash_html = ''.join([f'<div class="alert alert-{cat}">{msg}</div>' for cat, msg in session.pop('_flashes', [])])
    
    transactions_html = ""
    for trans in manual_transactions[:30]:
        debit_html = "".join([f"<div><span style='color: #28a745; font-weight: bold;'>💚 Dr.</span> {entry['account']}</div>" for entry in trans['debit_entries']])
        credit_html = "".join([f"<div><span style='color: #dc3545; font-weight: bold;'>❤️ Cr.</span> {entry['account']}</div>" for entry in trans['credit_entries']])
        balance_status = "✅" if abs(trans['total_debit'] - trans['total_credit']) < 0.01 else "⚠️"
        transactions_html += f"""
        <tr>
            <td>{trans['date']}</td>
            <td class="text-center"><code>{trans['ref_code']}</code></td>
            <td><strong>{trans['description']}</strong></td>
            <td>{debit_html or '-'}</td>
            <td>{credit_html or '-'}</td>
            <td class="text-right"><strong>{format_rupiah(trans['total_debit'])}</strong></td>
            <td class="text-right"><strong>{format_rupiah(trans['total_credit'])}</strong></td>
            <td class="text-center">{balance_status}</td>
        </tr>
        """
    if not transactions_html:
        transactions_html = '<tr><td colspan="8" class="text-center">📭 Belum ada transaksi manual</td></tr>'
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Transaksi Manual - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">📊</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Akuntan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/akuntan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/akuntan/accounts"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/manual-transaction" class="active"><span class="icon">➕</span> Transaksi Manual</a></li>
                    <li><a href="/akuntan/inventory-card"><span class="icon">📦</span> Inventory Card</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Penutupan</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Pembalikan</a></li>
                    <li><a href="/akuntan/assets"><span class="icon">🏢</span> Aset</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> NS</a></li>
                    <li><a href="/akuntan/adjusted-trial-balance"><span class="icon">✅</span> NS Penyesuaian</a></li>
                    <li><a href="/akuntan/worksheet"><span class="icon">📊</span> Neraca Lajur</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">💼</span> Lap. Keuangan</a></li>
                    <li><a href="/akuntan/cash-flow-statement"><span class="icon">💰</span> Arus Kas</a></li>
                    <li><a href="/akuntan/post-closing-trial-balance"><span class="icon">🔐</span> NS Penutupan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>📝 Transaksi Manual Akuntan</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section" style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; border-radius: 15px;">
                    <h2 style="color: white; margin-bottom: 10px;">➕ Input Transaksi Manual</h2>
                    <p style="opacity: 0.9; margin-bottom: 10px;">Catat transaksi yang belum dilakukan oleh kasir atau karyawan</p>
                    <div style="background: rgba(255,255,255,0.2); padding: 15px; border-radius: 8px; margin-top: 15px;">
                        <strong>📚 Aturan Debit-Kredit:</strong>
                        <ul style="margin: 10px 0 0 20px; opacity: 0.95;">
                            <li>🐟 Pembelian Bibit: Dr. Persediaan | Cr. Kas</li>
                            <li>📦 Pembelian Perlengkapan: Dr. Perlengkapan | Cr. Kas</li>
                            <li>🔧 Pembelian Peralatan: Dr. Peralatan | Cr. Kas</li>
                            <li>👨‍💼 Pembayaran Gaji: Dr. Beban Gaji | Cr. Kas</li>
                            <li>⚡ Pembayaran Listrik: Dr. Beban Listrik | Cr. Kas</li>
                            <li>💰 Penerimaan Kas: Dr. Kas | Cr. Pendapatan Lain-lain</li>
                        </ul>
                    </div>
                </div>
                
                <div class="content-section">
                    {flash_html}
                    <form method="POST">
                        <div class="form-row">
                            <div class="form-group">
                                <label>Tanggal Transaksi *</label>
                                <input type="date" name="date" required value="{datetime.now().strftime('%Y-%m-%d')}">
                            </div>
                            <div class="form-group">
                                <label>Jenis Transaksi *</label>
                                <select name="transaction_type" required id="transactionType" onchange="updatePreview()">
                                    <option value="">-- Pilih Jenis Transaksi --</option>
                                    <option value="pembelian_bibit">🐟 Pembelian Bibit Ikan</option>
                                    <option value="pembelian_perlengkapan">📦 Pembelian Perlengkapan</option>
                                    <option value="pembelian_peralatan">🔧 Pembelian Peralatan</option>
                                    <option value="pembayaran_gaji">👨‍💼 Pembayaran Gaji Karyawan</option>
                                    <option value="pembayaran_listrik">⚡ Pembayaran Listrik</option>
                                    <option value="penerimaan_kas">💰 Penerimaan Kas Lain-lain</option>
                                </select>
                            </div>
                        </div>
                        
                        <div class="form-group">
                            <label>Jumlah (Rp) *</label>
                            <input type="text" name="amount" required placeholder="Rp0,00" id="amountInput">
                        </div>
                        
                        <div class="form-group">
                            <label>Keterangan (Opsional)</label>
                            <textarea name="description" rows="2" placeholder="Keterangan tambahan..."></textarea>
                        </div>
                        
                        <!-- Preview Jurnal -->
                        <div id="jurnalPreview" style="background: #f8f9fa; padding: 20px; border-radius: 10px; margin-top: 20px; display: none;">
                            <h3 style="color: #667eea; margin-bottom: 15px;">📝 Preview Jurnal (Metode Perpetual)</h3>
                            <table style="width: 100%; background: white; border-collapse: collapse;">
                                <thead>
                                    <tr style="background: #667eea; color: white;">
                                        <th style="padding: 12px; text-align: left; width: 50%;">Akun</th>
                                        <th style="padding: 12px; text-align: right; width: 25%;">Debit</th>
                                        <th style="padding: 12px; text-align: right; width: 25%;">Kredit</th>
                                    </tr>
                                </thead>
                                <tbody id="previewBody">
                                </tbody>
                            </table>
                            <div style="margin-top: 15px; padding: 10px; background: #e8f5e9; border-radius: 5px; text-align: center;">
                                <strong style="color: #28a745;">✅ 2 akun akan masuk ke Jurnal Umum (1 Debit + 1 Kredit)</strong>
                            </div>
                        </div>
                        
                        <div id="bibitFields" style="display: none;">  <!-- Awal div yang akan ditampilkan/sembunyikan -->
                            <div class="form-row">
                                <div class="form-group">
                                    <label>Kuantitas (Kg) *</label>
                                    <input type="number" name="bibit_quantity" step="0.01" placeholder="0.00" id="bibitQuantity">
                                </div>
                                <div class="form-group">
                                    <label>Harga per Kg (Rp) *</label>
                                    <input type="text" name="bibit_price_per_kg" placeholder="Rp0,00" id="bibitPricePerKg">
                                </div>
                            </div>
                        </div>

                        <button type="submit" class="btn-sm btn-success btn-block" style="margin-top: 20px;">💾 Simpan ke Jurnal Umum</button>
                    </form>
                </div>
                
                <div class="content-section">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                        <div>
                            <h2 style="margin: 0;">📋 Riwayat Transaksi Manual</h2>
                            <p style="color: #666; margin: 5px 0 0 0; font-size: 14px;">✅ Semua transaksi sudah tercatat di <strong>Jurnal Umum</strong> dengan format Debit-Kredit</p>
                        </div>
                        <a href="/akuntan/journal-gj" class="btn-sm btn-primary">📖 Lihat Jurnal Umum</a>
                    </div>
                    <table>
                        <thead>
                            <tr style="background: #667eea; color: white;">
                                <th style="padding: 12px;">Tanggal</th>
                                <th class="text-center" style="padding: 12px;">Ref Code</th>
                                <th style="padding: 12px;">Keterangan</th>
                                <th style="padding: 12px;">Akun Debit (Dr.)</th>
                                <th style="padding: 12px;">Akun Kredit (Cr.)</th>
                                <th class="text-right" style="padding: 12px;">Total Debit</th>
                                <th class="text-right" style="padding: 12px;">Total Kredit</th>
                                <th class="text-center" style="padding: 12px;">Status</th>
                            </tr>
                        </thead>
                        <tbody>
                            {transactions_html}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        
        <script>
        document.getElementById('amountInput').addEventListener('input', function() {{
            updatePreview();
        }});
        
        document.getElementById('transactionType').addEventListener('change', function() {{
            var bibitFields = document.getElementById('bibitFields');
            if (this.value === 'pembelian_bibit') {{
                bibitFields.style.display = 'block'; // Tampilkan
            }} else {{
                bibitFields.style.display = 'none';  // Sembunyikan
            }}
        }});

        document.getElementById('amountInput').addEventListener('blur', function() {{
            let val = this.value.replace(/[^0-9]/g, '');
            if (val) {{
                let num = parseInt(val);
                this.value = 'Rp' + num.toLocaleString('id-ID') + ',00';
            }}
            updatePreview();
        }});
        
        document.getElementById('transactionType').addEventListener('change', function() {{
            updatePreview();
        }});
        
        function parseRupiah(str) {{
            if (!str) return 0;
            return parseFloat(str.replace(/Rp/g, "").replace(/\\./g, "").replace(",", ".")) || 0;
        }}
        
        function formatRupiah(num) {{
            return "Rp" + num.toLocaleString("id-ID", {{
                minimumFractionDigits: 2,
                maximumFractionDigits: 2
            }}).replace(",", "X").replace(".", ",").replace("X", ".");
        }}
        
        function updatePreview() {{
            const type = document.getElementById("transactionType").value;
            const amount = parseRupiah(document.getElementById("amountInput").value);
            const preview = document.getElementById("jurnalPreview");
            const body = document.getElementById("previewBody");
            
            if (!type || amount <= 0) {{
                preview.style.display = "none";
                return;
            }}
            
            // ✅ MAPPING SESUAI ATURAN AKUNTANSI
            const mapping = {{
                "pembelian_bibit": [
                    {{account: "1-1200 Persediaan Ikan Mujair", debit: amount, credit: 0}},
                    {{account: "1-1000 Kas", debit: 0, credit: amount}}
                ],
                "pembelian_perlengkapan": [
                    {{account: "1-1300 Perlengkapan", debit: amount, credit: 0}},
                    {{account: "1-1000 Kas", debit: 0, credit: amount}}
                ],
                "pembelian_peralatan": [
                    {{account: "1-2200 Peralatan", debit: amount, credit: 0}},
                    {{account: "1-1000 Kas", debit: 0, credit: amount}}
                ],
                "pembayaran_gaji": [
                    {{account: "6-1300 Beban Gaji", debit: amount, credit: 0}},
                    {{account: "1-1000 Kas", debit: 0, credit: amount}}
                ],
                "pembayaran_listrik": [
                    {{account: "6-1000 Beban Listrik", debit: amount, credit: 0}},
                    {{account: "1-1000 Kas", debit: 0, credit: amount}}
                ],
                "penerimaan_kas": [
                    {{account: "1-1000 Kas", debit: amount, credit: 0}},
                    {{account: "4-1201 Pendapatan Lain-lain", debit: 0, credit: amount}}
                ]
            }};
            
            const entries = mapping[type];
            if (!entries) {{
                preview.style.display = "none";
                return;
            }}
            
            body.innerHTML = "";
            entries.forEach((entry, index) => {{
                const isDebit = entry.debit > 0;
                const rowColor = index % 2 === 0 ? '#f8f9fa' : 'white';
                
                body.innerHTML += `
                    <tr style="background: ${{rowColor}}; border-bottom: 1px solid #dee2e6;">
                        <td style="padding: 12px;">
                            <strong style="color: ${{isDebit ? '#28a745' : '#dc3545'}};">
                                ${{isDebit ? '💚 Dr.' : '❤️ Cr.'}}
                            </strong>
                            <span style="margin-left: 8px;">${{entry.account}}</span>
                        </td>
                        <td class="text-right" style="padding: 12px; color: #28a745; font-weight: bold; font-size: 14px;">
                            ${{entry.debit > 0 ? formatRupiah(entry.debit) : "-"}}
                        </td>
                        <td class="text-right" style="padding: 12px; color: #dc3545; font-weight: bold; font-size: 14px;">
                            ${{entry.credit > 0 ? formatRupiah(entry.credit) : "-"}}
                        </td>
                    </tr>
                `;
            }});
            
            preview.style.display = "block";
        }}
        </script>
    </body>
    </html>
    """
    return html

@app.route('/akuntan/journal-<journal_type>/delete/<int:entry_id>', methods=['DELETE'])
def akuntan_delete_journal_entry(journal_type, entry_id):
    if 'username' not in session or session.get('role') != 'akuntan':
        return jsonify({'success': False})
    
    try:
        supabase.table('journal_entries').delete().eq('id', entry_id).execute()
        return jsonify({'success': True})
    except:
        return jsonify({'success': False})

@app.route('/akuntan/journal-<journal_type>/edit/<int:entry_id>', methods=['POST'])
def akuntan_edit_journal_entry(journal_type, entry_id):
    if 'username' not in session or session.get('role') != 'akuntan':
        return jsonify({'success': False})
    
    try:
        description = request.form.get('description')
        debit = parse_rupiah(request.form.get('debit', '0'))
        credit = parse_rupiah(request.form.get('credit', '0'))
        
        supabase.table('journal_entries').update({
            'description': description,
            'debit': float(debit),
            'credit': float(credit)
        }).eq('id', entry_id).execute()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# PERBAIKAN UNTUK JURNAL UMUM - WAJIB ISI DEBIT DAN KREDIT
# Ganti SELURUH fungsi akuntan_journal_gj() di app.py dengan ini:

@app.route('/akuntan/journal-gj', methods=['GET', 'POST'])
def akuntan_journal_gj():
    """Jurnal Umum (General Journal) - ENTRY GANDA WAJIB"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            date = request.form.get('date')
            ref_code = request.form.get('ref_code', 'GJ')
            
            # Ambil entry DEBIT
            debit_account = request.form.get('debit_account')
            debit_description = request.form.get('debit_description')
            debit_amount = parse_rupiah(request.form.get('debit_amount', '0'))
            
            # Ambil entry KREDIT
            credit_account = request.form.get('credit_account')
            credit_description = request.form.get('credit_description')
            credit_amount = parse_rupiah(request.form.get('credit_amount', '0'))
            
            # ✅ VALIDASI: KEDUANYA HARUS TERISI
            if not debit_account or not credit_account:
                flash('❌ Akun Debit dan Kredit harus dipilih!', 'error')
                return redirect(url_for('akuntan_journal_gj'))
            
            if debit_amount <= 0 or credit_amount <= 0:
                flash('❌ Jumlah Debit dan Kredit harus lebih dari 0!', 'error')
                return redirect(url_for('akuntan_journal_gj'))
            
            # ✅ VALIDASI: DEBIT = KREDIT
            if abs(debit_amount - credit_amount) > 0.01:
                flash(f'❌ Jurnal tidak balance! Debit: {format_rupiah(debit_amount)}, Kredit: {format_rupiah(credit_amount)}', 'error')
                return redirect(url_for('akuntan_journal_gj'))
            
            accounts = get_all_accounts()
            
            # Buat entry DEBIT
            debit_acc = next((a for a in accounts if a['account_code'] == debit_account), None)
            if debit_acc:
                create_journal_entry(
                    date=date,
                    account_code=debit_acc['account_code'],
                    account_name=debit_acc['account_name'],
                    description=debit_description,
                    debit=debit_amount,
                    credit=0,
                    journal_type='GJ',
                    ref_code=ref_code
                )
            
            # Buat entry KREDIT
            credit_acc = next((a for a in accounts if a['account_code'] == credit_account), None)
            if credit_acc:
                create_journal_entry(
                    date=date,
                    account_code=credit_acc['account_code'],
                    account_name=credit_acc['account_name'],
                    description=credit_description,
                    debit=0,
                    credit=credit_amount,
                    journal_type='GJ',
                    ref_code=ref_code
                )
            
            flash(f'✅ Jurnal berhasil disimpan! (2 entries)', 'success')
            return redirect(url_for('akuntan_journal_gj'))
            
        except Exception as e:
            flash(f'❌ Error: {str(e)}', 'error')
        
        return redirect(url_for('akuntan_journal_gj'))
    
    # ========== GET METHOD ==========
    username = session.get('username', 'User')
    journals = get_journal_entries(journal_type='GJ')
    accounts = get_all_accounts()
    
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    total_debit = sum(float(j.get('debit', 0)) for j in journals)
    total_credit = sum(float(j.get('credit', 0)) for j in journals)
    
    journals_html = ""
    for j in journals:
        journal_json = {
            'id': j['id'],
            'date': j['date'],
            'account_code': j['account_code'],
            'account_name': j['account_name'],
            'description': j['description'],
            'ref_code': j.get('ref_code', ''),
            'debit': j.get('debit', 0),
            'credit': j.get('credit', 0)
        }
        import json
        journal_data = json.dumps(journal_json).replace('"', '&quot;')
        
        journals_html += f"""
        <tr>
            <td>{j['date']}</td>
            <td class="text-center">{j['account_code']}</td>
            <td>{j['account_name']}</td>
            <td>{j['description']}</td>
            <td class="text-center">{j.get('ref_code', '-')}</td>
            <td class="text-right">{format_rupiah(j.get('debit', 0)) if j.get('debit', 0) > 0 else '-'}</td>
            <td class="text-right">{format_rupiah(j.get('credit', 0)) if j.get('credit', 0) > 0 else '-'}</td>
            <td class="text-center">
                <div class="btn-group">
                    <button class="btn-sm btn-warning" onclick='showEditModal({journal_data})' title="Edit">✏️</button>
                    <button class="btn-sm btn-danger" onclick="deleteJournal({j['id']}, '{j['description']}')" title="Hapus">🗑️</button>
                </div>
            </td>
        </tr>
        """
    
    accounts_options = "".join([f'<option value="{a["account_code"]}">{a["account_code"]} - {a["account_name"]}</option>' for a in accounts])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Jurnal Umum - Geboy Mujair</title>
        {generate_dashboard_style()}
        <style>
            .entry-box {{
                background: #f8f9fa;
                padding: 20px;
                border-radius: 10px;
                margin-bottom: 15px;
                border-left: 4px solid #667eea;
            }}
            .entry-box.debit {{ border-left-color: #28a745; }}
            .entry-box.credit {{ border-left-color: #dc3545; }}
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            {generate_sidebar('akuntan', username, 'journal')}
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Jurnal Umum (General Journal)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <h2>➕ Tambah Entry Jurnal (Debit & Kredit Wajib)</h2>
                    {flash_html}
                    <form method="POST" id="journalForm">
                        <div class="form-row">
                            <div class="form-group">
                                <label>Tanggal *</label>
                                <input type="date" name="date" required value="{datetime.now().strftime('%Y-%m-%d')}">
                            </div>
                            <div class="form-group">
                                <label>Ref Code</label>
                                <input type="text" name="ref_code" placeholder="GJ" value="GJ">
                            </div>
                        </div>
                        
                        <!-- ENTRY DEBIT -->
                        <div class="entry-box debit">
                            <h3 style="color: #28a745; margin-bottom: 15px;">💚 DEBIT (Dr.)</h3>
                            <div class="form-group">
                                <label>Akun Debit *</label>
                                <select name="debit_account" required>
                                    <option value="">-- Pilih Akun Debit --</option>
                                    {accounts_options}
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Keterangan Debit *</label>
                                <textarea name="debit_description" required rows="2" placeholder="Deskripsi transaksi..."></textarea>
                            </div>
                            <div class="form-group">
                                <label>Jumlah Debit *</label>
                                <input type="text" name="debit_amount" required placeholder="Rp0,00" class="rupiah-input" id="debitInput">
                            </div>
                        </div>
                        
                        <!-- ENTRY KREDIT -->
                        <div class="entry-box credit">
                            <h3 style="color: #dc3545; margin-bottom: 15px;">❤️ KREDIT (Cr.)</h3>
                            <div class="form-group">
                                <label>Akun Kredit *</label>
                                <select name="credit_account" required>
                                    <option value="">-- Pilih Akun Kredit --</option>
                                    {accounts_options}
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Keterangan Kredit *</label>
                                <textarea name="credit_description" required rows="2" placeholder="Deskripsi transaksi..."></textarea>
                            </div>
                            <div class="form-group">
                                <label>Jumlah Kredit *</label>
                                <input type="text" name="credit_amount" required placeholder="Rp0,00" class="rupiah-input" id="creditInput">
                            </div>
                        </div>
                        
                        <!-- PREVIEW BALANCE -->
                        <div style="background: #667eea; color: white; padding: 15px; border-radius: 10px; margin-top: 20px;">
                            <div style="display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; text-align: center;">
                                <div>
                                    <strong>Debit:</strong><br>
                                    <span id="previewDebit" style="font-size: 20px;">Rp0,00</span>
                                </div>
                                <div>
                                    <strong>Kredit:</strong><br>
                                    <span id="previewCredit" style="font-size: 20px;">Rp0,00</span>
                                </div>
                                <div>
                                    <strong>Status:</strong><br>
                                    <span id="balanceStatus" style="font-size: 20px;">⚖️ -</span>
                                </div>
                            </div>
                        </div>
                        
                        <button type="submit" class="btn-sm btn-success btn-block" style="margin-top: 20px;">💾 Simpan Jurnal</button>
                    </form>
                </div>
                
                <div class="content-section">
                    <h2>📝 General Journal</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Tanggal</th>
                                <th class="text-center">Kode</th>
                                <th>Akun</th>
                                <th>Keterangan</th>
                                <th class="text-center">Ref</th>
                                <th class="text-right">Debit</th>
                                <th class="text-right">Kredit</th>
                                <th class="text-center">Aksi</th>
                            </tr>
                        </thead>
                        <tbody>
                            {journals_html if journals_html else '<tr><td colspan="8" class="text-center">Belum ada entry</td></tr>'}
                        </tbody>
                        {f'''<tfoot style="background: #f8f9fa; font-weight: bold;">
                            <tr>
                                <td colspan="5" class="text-right" style="padding: 15px;">TOTAL:</td>
                                <td class="text-right" style="padding: 15px; color: #28a745;">{format_rupiah(total_debit)}</td>
                                <td class="text-right" style="padding: 15px; color: #dc3545;">{format_rupiah(total_credit)}</td>
                                <td></td>
                            </tr>
                        </tfoot>''' if journals else ''}
                    </table>
                </div>
            </div>
        </div>
        
        <script>
        // Format rupiah
        document.querySelectorAll('.rupiah-input').forEach(input => {{
            input.addEventListener('blur', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = 'Rp' + parseInt(val).toLocaleString('id-ID') + ',00';
                }}
                updatePreview();
            }});
            
            input.addEventListener('focus', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = val;
                }}
            }});
            
            input.addEventListener('input', updatePreview);
        }});
        
        function parseRupiah(str) {{
            if (!str) return 0;
            return parseFloat(str.replace(/Rp/g, '').replace(/\\./g, '').replace(',', '.')) || 0;
        }}
        
        function formatRupiah(num) {{
            return 'Rp' + num.toLocaleString('id-ID', {{
                minimumFractionDigits: 2,
                maximumFractionDigits: 2
            }}).replace(',', 'X').replace('.', ',').replace('X', '.');
        }}
        
        function updatePreview() {{
            const debitVal = parseRupiah(document.getElementById('debitInput').value);
            const creditVal = parseRupiah(document.getElementById('creditInput').value);
            
            document.getElementById('previewDebit').textContent = formatRupiah(debitVal);
            document.getElementById('previewCredit').textContent = formatRupiah(creditVal);
            
            const diff = Math.abs(debitVal - creditVal);
            const status = document.getElementById('balanceStatus');
            
            if (debitVal === 0 && creditVal === 0) {{
                status.textContent = '⚖️ -';
                status.style.color = 'white';
            }} else if (diff < 0.01) {{
                status.textContent = '✅ BALANCE';
                status.style.color = '#28a745';
            }} else {{
                status.textContent = '❌ NOT BALANCE';
                status.style.color = '#ffc107';
            }}
        }}
        
        // Validasi sebelum submit
        document.getElementById('journalForm').addEventListener('submit', function(e) {{
            const debitVal = parseRupiah(document.getElementById('debitInput').value);
            const creditVal = parseRupiah(document.getElementById('creditInput').value);
            
            if (debitVal <= 0 || creditVal <= 0) {{
                e.preventDefault();
                alert('❌ Debit dan Kredit harus diisi dengan nilai lebih dari 0!');
                return false;
            }}
            
            if (Math.abs(debitVal - creditVal) > 0.01) {{
                e.preventDefault();
                alert('❌ Debit dan Kredit harus SAMA!\\n\\nDebit: ' + formatRupiah(debitVal) + '\\nKredit: ' + formatRupiah(creditVal));
                return false;
            }}
        }});
        
        function deleteJournal(entryId, description) {{
            if (!confirm('⚠️ Yakin ingin menghapus jurnal?\\n\\n' + description)) {{
                return;
            }}
            
            fetch('/akuntan/journal-gj/delete/' + entryId, {{
                method: 'DELETE'
            }})
            .then(res => res.json())
            .then(data => {{
                if (data.success) {{
                    alert('✅ Jurnal berhasil dihapus!');
                    location.reload();
                }} else {{
                    alert('❌ Error: ' + data.message);
                }}
            }});
        }}
        </script>
    </body>
    </html>
    """
    
    return html


@app.route('/akuntan/journal-gj/edit/<int:entry_id>', methods=['GET'])
def akuntan_edit_journal_form(entry_id):
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))

    # Ambil data jurnal
    res = supabase.table('journal_entries').select('*').eq('id', entry_id).execute()
    if not res.data:
        flash("❌ Jurnal tidak ditemukan!", "error")
        return redirect(url_for('akuntan_journal_gj'))

    entry = res.data[0]
    accounts = get_all_accounts()
    username = session.get('username')

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Edit Jurnal Umum</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            {generate_sidebar('akuntan', username, 'journal-gj')}
            <div class="main-content">

                <div class="top-bar">
                    <h1>Edit Jurnal #{entry_id}</h1>
                </div>

                <div class="content-section">
                    <form method="POST" action="/akuntan/journal-gj/edit">

                        <input type="hidden" name="entry_id" value="{entry_id}">

                        <label>Tanggal</label>
                        <input type="date" name="date" required value="{entry['date']}">

                        <label>Kode Akun</label>
                        <select name="account_code" required>
                            {''.join([f"<option value='{a['account_code']}' {'selected' if a['account_code']==entry['account_code'] else ''}>{a['account_code']} - {a['account_name']}</option>" for a in accounts])}
                        </select>

                        <label>Deskripsi</label>
                        <input type="text" name="description" value="{entry['description']}">

                        <label>Debit</label>
                        <input type="text" name="debit" value="{format_rupiah(entry['debit']) if entry['debit']>0 else ''}">

                        <label>Kredit</label>
                        <input type="text" name="credit" value="{format_rupiah(entry['credit']) if entry['credit']>0 else ''}">

                        <label>Kode Referensi</label>
                        <input type="text" name="ref_code" value="{entry.get('ref_code','GJ')}">

                        <div style="margin-top:20px; display:grid; grid-template-columns:1fr 1fr; gap:10px;">
                            <a class="btn-sm btn-secondary btn-block" href="/akuntan/journal-gj">↩️ Batal</a>
                            <button class="btn-sm btn-success btn-block" type="submit">💾 Simpan</button>
                        </div>
                    </form>
                </div>

            </div>
        </div>
    </body>
    </html>"""
    return html


@app.route('/akuntan/journal-gj/edit/<int:entry_id>', methods=['POST'])
def akuntan_edit_journal_gj(entry_id):
    """Update jurnal entry"""
    if 'username' not in session or session.get('role') != 'akuntan':
        flash('❌ Unauthorized', 'error')
        return redirect(url_for('login'))

    try:
        date = request.form.get('date')
        account_code = request.form.get('account_code')
        description = request.form.get('description')
        debit = parse_rupiah(request.form.get('debit') or '0')
        credit = parse_rupiah(request.form.get('credit') or '0')
        ref_code = request.form.get('ref_code', 'GJ')

        # Validasi debit & kredit
        if debit > 0 and credit > 0:
            flash("❌ Tidak boleh isi debit dan kredit sekaligus!", "error")
            return redirect(url_for('akuntan_journal_gj'))

        if debit == 0 and credit == 0:
            flash("❌ Harus isi debit atau kredit!", "error")
            return redirect(url_for('akuntan_journal_gj'))

        # Ambil nama akun
        accounts = get_all_accounts()
        account = next((a for a in accounts if a['account_code'] == account_code), None)
        if not account:
            flash("❌ Kode akun tidak valid!", "error")
            return redirect(url_for('akuntan_journal_gj'))

        update_data = {
            'date': date,
            'account_code': account_code,
            'account_name': account['account_name'],
            'description': description,
            'debit': float(debit),
            'credit': float(credit),
            'ref_code': ref_code,
            'updated_at': datetime.now().isoformat()
        }

        response = supabase.table('journal_entries').update(update_data).eq('id', entry_id).execute()

        flash("✅ Jurnal berhasil diperbarui!", "success")

    except Exception as e:
        flash(f"❌ Error: {str(e)}", "error")

    return redirect(url_for('akuntan_journal_gj'))

@app.route('/akuntan/journal-gj/delete/<int:entry_id>', methods=['DELETE'])
def akuntan_delete_journal_gj(entry_id):
    """Delete jurnal entry"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        # Cek apakah entry ada
        check = supabase.table('journal_entries').select('*').eq('id', entry_id).execute()
        
        if not check.data:
            return jsonify({'success': False, 'message': 'Jurnal tidak ditemukan'})
        
        # Hapus entry
        supabase.table('journal_entries').delete().eq('id', entry_id).execute()
        
        return jsonify({'success': True, 'message': 'Jurnal berhasil dihapus'})
        
    except Exception as e:
        print(f"ERROR DELETE JOURNAL: {e}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/akuntan/ledger')
def akuntan_ledger():
    """Buku Besar (General Ledger) - Tampilkan Semua Akun"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    accounts = get_all_accounts()
    
    # Generate ledger untuk semua akun
    all_ledgers_html = ""
    
    for account in accounts:
        code = account['account_code']
        name = account['account_name']
        normal_balance = account['normal_balance']
        
        # Ambil semua jurnal entries untuk akun ini
        entries = [e for e in get_journal_entries() if e['account_code'] == code]
        
        # Skip akun yang tidak ada transaksi
        if not entries and account.get('beginning_balance', 0) == 0:
            continue
        
        balance = float(account.get('beginning_balance', 0))
        
        # Generate tabel untuk akun ini
        entries_html = f"""
        <tr style="background: #f8f9fa; font-weight: bold;">
            <td colspan="5">Saldo Awal</td>
            <td class="text-right">{format_rupiah(balance)}</td>
        </tr>
        """
        
        for entry in entries:
            debit = float(entry.get('debit', 0))
            credit = float(entry.get('credit', 0))
            
            if normal_balance == 'debit':
                balance += debit - credit
            else:
                balance += credit - debit
            
            entries_html += f"""
            <tr>
                <td>{entry['date']}</td>
                <td>{entry['description']}</td>
                <td class="text-center">{entry.get('ref_code', '-')}</td>
                <td class="text-right">{format_rupiah(debit) if debit > 0 else '-'}</td>
                <td class="text-right">{format_rupiah(credit) if credit > 0 else '-'}</td>
                <td class="text-right"><strong>{format_rupiah(balance)}</strong></td>
            </tr>
            """
        
        entries_html += f"""
        <tr style="background: #667eea; color: white; font-weight: bold;">
            <td colspan="5" class="text-right" style="padding: 12px;">SALDO AKHIR:</td>
            <td class="text-right" style="padding: 12px; font-size: 16px;">{format_rupiah(balance)}</td>
        </tr>
        """
        
        all_ledgers_html += f"""
        <div class="content-section" style="margin-bottom: 30px;">
            <div style="background: #667eea; color: white; padding: 15px; border-radius: 10px 10px 0 0; margin-bottom: 0;">
                <h3 style="margin: 0; display: flex; justify-content: space-between; align-items: center;">
                    <span>{code} - {name}</span>
                    <span style="font-size: 14px; opacity: 0.9;">Saldo Normal: {normal_balance.title()}</span>
                </h3>
            </div>
            <div style="overflow-x: auto;">
                <table style="margin-top: 0;">
                    <thead>
                        <tr>
                            <th>Tanggal</th>
                            <th>Keterangan</th>
                            <th class="text-center">Ref</th>
                            <th class="text-right">Debit</th>
                            <th class="text-right">Kredit</th>
                            <th class="text-right">Saldo</th>
                        </tr>
                    </thead>
                    <tbody>
                        {entries_html}
                    </tbody>
                </table>
            </div>
        </div>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Buku Besar - Geboy Mujair</title>
        {generate_dashboard_style()}
        <style>
            /* Smooth scroll */
            html {{
                scroll-behavior: smooth;
            }}
            
            /* Quick navigation */
            .quick-nav {{
                position: sticky;
                top: 20px;
                background: white;
                padding: 15px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                margin-bottom: 30px;
                z-index: 100;
            }}
            
            .quick-nav h3 {{
                color: #667eea;
                margin-bottom: 15px;
                font-size: 16px;
            }}
            
            .quick-nav-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
                gap: 10px;
            }}
            
            .quick-nav-item {{
                padding: 8px 12px;
                background: #f8f9fa;
                border-radius: 5px;
                text-decoration: none;
                color: #333;
                font-size: 13px;
                transition: all 0.3s;
                border: 2px solid transparent;
            }}
            
            .quick-nav-item:hover {{
                background: #667eea;
                color: white;
                border-color: #667eea;
                transform: translateX(5px);
            }}
            
            /* Print styles */
            @media print {{
                .sidebar, .top-bar, .quick-nav, .no-print {{
                    display: none !important;
                }}
                .main-content {{
                    margin-left: 0;
                    width: 100%;
                    padding: 20px;
                }}
                .content-section {{
                    page-break-inside: avoid;
                    margin-bottom: 40px;
                }}
            }}
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">📊</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Akuntan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/akuntan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/akuntan/accounts"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/manual-transaction"><span class="icon">➕</span> Transaksi Manual</a></li>
                    <li><a href="/akuntan/inventory-card"><span class="icon">📦</span> Inventory Card</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Penutupan</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Pembalikan</a></li>
                    <li><a href="/akuntan/assets"><span class="icon">🏢</span> Aset</a></li>
                    <li><a href="/akuntan/ledger" class="active"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> NS</a></li>
                    <li><a href="/akuntan/adjusted-trial-balance"><span class="icon">✅</span> NS Penyesuaian</a></li>
                    <li><a href="/akuntan/worksheet"><span class="icon">📊</span> Neraca Lajur</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">💼</span> Lap. Keuangan</a></li>
                    <li><a href="/akuntan/cash-flow-statement"><span class="icon">💰</span> Arus Kas</a></li>
                    <li><a href="/akuntan/post-closing-trial-balance"><span class="icon">📄</span> NS Penutupan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Buku Besar (General Ledger)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <!-- QUICK NAVIGATION -->
                <div class="quick-nav no-print">
                    <h3>🔍 Quick Navigation - Lompat ke Akun:</h3>
                    <div class="quick-nav-grid">
                        {' '.join([f'<a href="#account-{acc["account_code"].replace("-", "_")}" class="quick-nav-item">{acc["account_code"]} - {acc["account_name"]}</a>' for acc in accounts if any(e['account_code'] == acc['account_code'] for e in get_journal_entries()) or acc.get('beginning_balance', 0) != 0])}
                    </div>
                </div>
                
                <!-- INFO -->
                <div class="content-section no-print" style="background: #d1ecf1; border-left: 4px solid #17a2b8;">
                    <h3 style="color: #0c5460; margin-bottom: 10px;">ℹ️ Informasi Buku Besar</h3>
                    <p style="color: #0c5460; line-height: 1.8; margin: 0;">
                        Buku besar menampilkan <strong>semua akun yang memiliki transaksi</strong> atau saldo awal.<br>
                        Gunakan <strong>Quick Navigation</strong> di atas untuk melompat ke akun tertentu.<br>
                        Klik tombol <strong>Cetak</strong> untuk mencetak semua buku besar sekaligus.
                    </p>
                </div>
                
                <!-- SEMUA BUKU BESAR -->
                {all_ledgers_html if all_ledgers_html else '''
                <div class="content-section" style="text-align: center; padding: 60px 20px;">
                    <div style="font-size: 60px; margin-bottom: 20px;">📚</div>
                    <h3 style="color: #666; margin-bottom: 10px;">Belum Ada Transaksi</h3>
                    <p style="color: #999;">Buat jurnal entry terlebih dahulu untuk melihat buku besar</p>
                </div>
                '''}
                
                <!-- BUTTON CETAK -->
                <div class="content-section no-print">
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px;">
                        <button onclick="window.print()" class="btn-sm btn-primary btn-block">
                            🖨️ Cetak Semua Buku Besar
                        </button>
                        <button onclick="window.scrollTo({{top: 0, behavior: 'smooth'}})" class="btn-sm btn-info btn-block">
                            ⬆️ Kembali ke Atas
                        </button>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
        // Update datetime
        function updateDateTime() {{
            const now = new Date();
            const options = {{ 
                weekday: 'long', 
                year: 'numeric', 
                month: 'long', 
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            }};
            const elem = document.getElementById('datetime');
            if (elem) elem.textContent = now.toLocaleDateString('id-ID', options);
        }}
        setInterval(updateDateTime, 1000);
        updateDateTime();
        
        // Highlight akun yang sedang dilihat di quick nav
        const observer = new IntersectionObserver((entries) => {{
            entries.forEach(entry => {{
                if (entry.isIntersecting) {{
                    // Remove active class from all
                    document.querySelectorAll('.quick-nav-item').forEach(item => {{
                        item.style.background = '#f8f9fa';
                        item.style.color = '#333';
                    }});
                    
                    // Add active class to current
                    const accountCode = entry.target.id.replace('account-', '').replace('_', '-');
                    const navItem = document.querySelector(`a[href="#${{entry.target.id}}"]`);
                    if (navItem) {{
                        navItem.style.background = '#667eea';
                        navItem.style.color = 'white';
                    }}
                }}
            }});
        }}, {{
            threshold: 0.5
        }});
        
        // Observe all ledger sections
        document.querySelectorAll('.content-section[id^="account-"]').forEach(section => {{
            observer.observe(section);
        }});
        </script>
    </body>
    </html>
    """
    
    return html
# ============== ROUTES - INVENTORY CARD ==============
@app.route('/akuntan/inventory-card')
def akuntan_inventory_card():
    """Halaman Inventory Card - Struktur Lama"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username')
    
    # Ambil semua inventory card
    inventory_card = supabase.table('inventory_card')\
        .select('*')\
        .order('date', desc=False)\
        .order('id', desc=False)\
        .execute()
    
    card = inventory_card.data if inventory_card.data else []
    
    # Generate HTML Table
    inventory_html = ""
    for card in card:
        # Hitung amount untuk Purchase dan Sales
        # BARIS YANG SUDAH DIPERBAIKI DAN AMAN
        quantity_in = card.get('quantity_in') or 0
        unit_price = card.get('unit_price') or 0
        purchase_amount = float(quantity_in) * float(unit_price)
        quantity_out = card.get('quantity_out') or 0
        unit_price_for_sales = card.get('unit_price') or 0 # Gunakan nama variabel berbeda jika perlu
        sales_amount = float(quantity_out) * float(unit_price_for_sales)
        balance_qty = card.get('balance_quantity') or 0
        balance_price = card.get('balance_unit_price') or 0
        balance_amount = card.get('balance_amount') or 0 
        
        inventory_html += f"""
        <tr>
            <td class="text-center">{card.get('date', '')}</td>
            <td class="text-center">{card.get('ref_code', '-')}</td>
            <td>{card.get('description', '')}</td>
            
            <!-- PURCHASE (quantity_in) -->
            <td class="text-center">{card.get('quantity_in', 0) if card.get('quantity_in', 0) > 0 else ''}</td>
            <td class="text-right">{format_rupiah(card.get('unit_price', 0)) if card.get('quantity_in', 0) > 0 else ''}</td>
            <td class="text-right">{format_rupiah(purchase_amount) if card.get('quantity_in', 0) > 0 else ''}</td>
            
            <!-- SALES (quantity_out) -->
            <td class="text-center">{card.get('quantity_out', 0) if card.get('quantity_out', 0) > 0 else ''}</td>
            <td class="text-right">{format_rupiah(card.get('unit_price', 0)) if card.get('quantity_out', 0) > 0 else ''}</td>
            <td class="text-right">{format_rupiah(sales_amount) if card.get('quantity_out', 0) > 0 else ''}</td>
            
            <!-- BALANCE -->
            <td class="text-center"><strong>{card.get('balance_quantity', 0)}</strong></td>
            <td class="text-right"><strong>{format_rupiah(card.get('unit_price', 0))}</strong></td>
            <td class="text-right"><strong>{format_rupiah(balance_amount)}</strong></td>
            
            <td class="text-center">
                <button class="btn-sm btn-warning" onclick="editInventory({card['id']}, {card.get('unit_price', 0)})" title="Edit HPP">✏️</button>
                <button class="btn-sm btn-danger" onclick="deleteInventory({card['id']})" title="Hapus">🗑️</button>
            </td>
        </tr>
        """
    
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <title>Inventory Card - Geboy Mujair</title>
        {generate_dashboard_style()}
        <style>
            .inventory-table {{
                font-size: 11px;
            }}
            .inventory-table th {{
                background: #667eea;
                color: white;
                padding: 8px 5px;
                text-align: center;
                border: 1px solid #ddd;
            }}
            .inventory-table td {{
                padding: 6px 5px;
                border: 1px solid #ddd;
            }}
            .section-header {{
                background: #f8f9fa;
                font-weight: bold;
                text-align: center;
            }}
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            {generate_sidebar('akuntan', username, 'inventory-card')}
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>📦 Inventory Card - Metode Perpetual</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                {flash_html}
                
                <div class="content-section">
                    <h2>Kartu Persediaan Ikan Mujair</h2>
                    <div style="overflow-x: auto;">
                        <table class="inventory-table" style="width: 100%; border-collapse: collapse;">
                            <thead>
                                <tr>
                                    <th rowspan="2">Date</th>
                                    <th rowspan="2">Ref Code</th>
                                    <th rowspan="2">Description</th>
                                    <th colspan="3" class="section-header">Purchase</th>
                                    <th colspan="3" class="section-header">Sales</th>
                                    <th colspan="3" class="section-header">Balance</th>
                                    <th rowspan="2">Action</th>
                                </tr>
                                <tr>
                                    <!-- Purchase -->
                                    <th>Quantity</th>
                                    <th>Unit price</th>
                                    <th>Amount</th>
                                    <!-- Sales -->
                                    <th>Quantity</th>
                                    <th>Unit price</th>
                                    <th>Amount</th>
                                    <!-- Balance -->
                                    <th>Quantity</th>
                                    <th>Unit price</th>
                                    <th>Amount</th>
                                </tr>
                            </thead>
                            <tbody>
                                {inventory_html if inventory_html else '<tr><td colspan="13" class="text-center">Belum ada data inventory</td></tr>'}
                            </tbody>
                        </table>
                    </div>
                    
                    <div style="margin-top: 20px;">
                        <button class="btn-sm btn-primary" onclick="showAddModal()">➕ Tambah Entry Manual</button>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Modal Edit HPP -->
        <div id="editModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:9999;">
            <div style="background:white; width:400px; margin:100px auto; padding:30px; border-radius:10px;">
                <h3>Edit Unit Price (HPP)</h3>
                <input type="hidden" id="editId">
                <div class="form-group">
                    <label>Unit Price Baru:</label>
                    <input type="text" id="editPrice" class="rupiah-input" placeholder="Rp0,00">
                </div>
                <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:20px;">
                    <button class="btn-sm btn-secondary btn-block" onclick="closeEditModal()">Batal</button>
                    <button class="btn-sm btn-success btn-block" onclick="saveEditInventory()">Simpan</button>
                </div>
            </div>
        </div>
        
        <!-- Modal Add Entry -->
        <div id="addModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:9999; overflow-y:auto;">
            <div style="background:white; width:600px; margin:50px auto; padding:30px; border-radius:10px;">
                <h3>➕ Tambah Entry Manual</h3>
                <form id="addForm">
                    <div class="form-group">
                        <label>Tanggal *</label>
                        <input type="date" id="addDate" required value="{datetime.now().strftime('%Y-%m-%d')}">
                    </div>
                    <div class="form-group">
                        <label>Ref Code *</label>
                        <input type="text" id="addRefCode" required placeholder="MANUAL-001">
                    </div>
                    <div class="form-group">
                        <label>Keterangan *</label>
                        <input type="text" id="addDescription" required placeholder="Deskripsi transaksi">
                    </div>
                    <div class="form-group">
                        <label>Product Name *</label>
                        <input type="text" id="addProductName" required value="Ikan Mujair">
                    </div>
                    <div class="form-row">
                        <div class="form-group">
                            <label>Quantity In</label>
                            <input type="number" id="addQuantityIn" step="0.01" min="0" value="0" placeholder="0">
                        </div>
                        <div class="form-group">
                            <label>Quantity Out</label>
                            <input type="number" id="addQuantityOut" step="0.01" min="0" value="0" placeholder="0">
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Unit Price *</label>
                        <input type="text" id="addUnitPrice" required placeholder="Rp0,00" class="rupiah-input">
                    </div>
                    <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:20px;">
                        <button type="button" class="btn-sm btn-secondary btn-block" onclick="closeAddModal()">Batal</button>
                        <button type="submit" class="btn-sm btn-success btn-block">Simpan</button>
                    </div>
                </form>
            </div>
        </div>
        
        <script>
        function showAddModal() {{
            document.getElementById('addModal').style.display = 'block';
        }}
        
        function closeAddModal() {{
            document.getElementById('addModal').style.display = 'none';
        }}
        
        document.getElementById('addForm').addEventListener('submit', function(e) {{
            e.preventDefault();
            
            const data = {{
                date: document.getElementById('addDate').value,
                ref_code: document.getElementById('addRefCode').value,
                description: document.getElementById('addDescription').value,
                product_name: document.getElementById('addProductName').value,
                quantity_in: parseFloat(document.getElementById('addQuantityIn').value) || 0,
                quantity_out: parseFloat(document.getElementById('addQuantityOut').value) || 0,
                unit_price: parseFloat(document.getElementById('addUnitPrice').value.replace(/[^0-9]/g, '')) || 0
            }};
            
            fetch('/akuntan/inventory-card/add', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify(data)
            }})
            .then(res => res.json())
            .then(result => {{
                if(result.success) {{
                    alert('✅ Entry berhasil ditambahkan!');
                    location.reload();
                }} else {{
                    alert('❌ Error: ' + result.message);
                }}
            }});
        }});
        
        function editInventory(id, currentPrice) {{
            document.getElementById('editId').value = id;
            document.getElementById('editPrice').value = 'Rp' + currentPrice.toLocaleString('id-ID') + ',00';
            document.getElementById('editModal').style.display = 'block';
        }}
        
        function closeEditModal() {{
            document.getElementById('editModal').style.display = 'none';
        }}
        
        function saveEditInventory() {{
            const id = document.getElementById('editId').value;
            const priceStr = document.getElementById('editPrice').value;
            const price = parseFloat(priceStr.replace(/Rp/g, '').replace(/\\./g, '').replace(',', '.')) || 0;
            
            fetch('/akuntan/inventory-card/edit/' + id, {{
                method: 'POST',
                headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{unit_price: price}})
            }})
            .then(res => res.json())
            .then(data => {{
                if(data.success) {{
                    alert('✅ HPP berhasil diupdate!');
                    location.reload();
                }} else {{
                    alert('❌ Error: ' + data.message);
                }}
            }});
        }}
        
        function deleteInventory(id) {{
            if(!confirm('⚠️ Yakin ingin menghapus entry ini?')) return;
            
            fetch('/akuntan/inventory-card/delete/' + id, {{
                method: 'DELETE'
            }})
            .then(res => res.json())
            .then(data => {{
                if(data.success) {{
                    alert('✅ Entry berhasil dihapus!');
                    location.reload();
                }} else {{
                    alert('❌ Error: ' + data.message);
                }}
            }});
        }}
        
        // Format rupiah
        document.querySelectorAll('.rupiah-input').forEach(input => {{
            input.addEventListener('blur', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = 'Rp' + parseInt(val).toLocaleString('id-ID') + ',00';
                }}
            }});
            
            input.addEventListener('focus', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = val;
                }}
            }});
        }});
        </script>
    </body>
    </html>
    """
    
    return html

@app.route('/akuntan/inventory-card/edit/<int:card_id>', methods=['POST'])
def akuntan_edit_inventory_card(card_id):
    """Edit Unit Price di Inventory Card"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        data = request.get_json()
        new_unit_price = float(data.get('unit_price', 0))
        
        # Update unit price
        supabase.table('inventory_card').update({
            'unit_price': new_unit_price
        }).eq('id', card_id).execute()
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/akuntan/inventory-card/delete/<int:card_id>', methods=['DELETE'])
def akuntan_delete_inventory_card(card_id):
    """Delete inventory card entry"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    try:
        success = delete_inventory_card(card_id)
        
        if success:
            return jsonify({'success': True, 'message': 'Entry berhasil dihapus'}), 200
        else:
            return jsonify({'success': False, 'message': 'Gagal menghapus entry'}), 500
            
    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/akuntan/trial-balance')
def akuntan_trial_balance():
    """Neraca Saldo (Trial Balance)"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    trial_balance = get_trial_balance()
    
    total_debit = sum(float(tb['debit']) for tb in trial_balance)
    total_credit = sum(float(tb['credit']) for tb in trial_balance)
    is_balanced = abs(total_debit - total_credit) < 0.01
    
    tb_html = ""
    for tb in trial_balance:
        tb_html += f"""
        <tr>
            <td class="text-center">{tb['account_code']}</td>
            <td>{tb['account_name']}</td>
            <td class="text-right">{format_rupiah(tb['debit'])}</td>
            <td class="text-right">{format_rupiah(tb['credit'])}</td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Neraca Saldo - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">📊</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Akuntan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/akuntan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/akuntan/accounts"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/manual-transaction"><span class="icon">➕</span> Transaksi Manual</a></li>
                    <li><a href="/akuntan/inventory-card"><span class="icon">📦</span> Inventory Card</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Penutupan</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Pembalikan</a></li>
                    <li><a href="/akuntan/assets"><span class="icon">🏢</span> Aset</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> NS</a></li>
                    <li><a href="/akuntan/adjusted-trial-balance"><span class="icon">✅</span> NS Penyesuaian</a></li>
                    <li><a href="/akuntan/worksheet"><span class="icon">📊</span> Neraca Lajur</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">💼</span> Lap. Keuangan</a></li>
                    <li><a href="/akuntan/cash-flow-statement"><span class="icon">💰</span> Arus Kas</a></li>
                    <li><a href="/akuntan/post-closing-trial-balance"><span class="icon">🔐</span> NS Penutupan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Neraca Saldo (Trial Balance)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <div style="text-align: center; margin-bottom: 30px;">
                        <h2 style="color: #667eea; margin-bottom: 5px;">GEBOY MUJAIR</h2>
                        <h3 style="color: #333; margin-bottom: 5px;">NERACA SALDO</h3>
                        <p style="color: #666;">Per {datetime.now().strftime('%d %B %Y')}</p>
                    </div>
                    
                    <table>
                        <thead>
                            <tr>
                                <th class="text-center">Kode Akun</th>
                                <th>Nama Akun</th>
                                <th class="text-right">Debit</th>
                                <th class="text-right">Kredit</th>
                            </tr>
                        </thead>
                        <tbody>
                            {tb_html if tb_html else '<tr><td colspan="4" class="text-center">Tidak ada data</td></tr>'}
                        </tbody>
                        {f'''<tfoot style="background: {'#d4edda' if is_balanced else '#f8d7da'}; font-weight: bold;">
                            <tr>
                                <td colspan="2" class="text-right" style="padding: 15px; font-size: 16px;">TOTAL:</td>
                                <td class="text-right" style="padding: 15px; font-size: 16px; color: #667eea;">{format_rupiah(total_debit)}</td>
                                <td class="text-right" style="padding: 15px; font-size: 16px; color: #dc3545;">{format_rupiah(total_credit)}</td>
                            </tr>
                            <tr style="background: {'#d4edda' if is_balanced else '#f8d7da'};">
                                <td colspan="4" class="text-center" style="padding: 15px; font-size: 18px; color: {'#155724' if is_balanced else '#721c24'};">
                                    {'✓ BALANCE - Debit dan Kredit Seimbang!' if is_balanced else '✗ NOT BALANCE - Debit dan Kredit Tidak Seimbang!'}
                                </td>
                            </tr>
                        </tfoot>
                        ''' if trial_balance else ''}
                    </table>
                </div>
                
                <div class="content-section no-print">
                    <button onclick="window.print()" class="btn-sm btn-primary btn-block">🖨️ Cetak Neraca Saldo</button>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html

@app.route('/akuntan/adjusted-trial-balance')
def akuntan_adjusted_trial_balance():
    """Neraca Saldo Setelah Penyesuaian (dari Neraca Lajur)"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Ambil data dari worksheet (neraca lajur)
    accounts = get_all_accounts()
    adjustment_journals = get_journal_entries(journal_type='AJ')
    
    trial_balance = []
    
    for account in accounts:
        code = account['account_code']
        name = account['account_name']
        normal_balance = account['normal_balance']
        
        # Saldo sebelum penyesuaian
        balance_before = get_ledger_balance(code)
        
        # Penyesuaian
        adj_debit = sum(float(j.get('debit', 0)) for j in adjustment_journals if j['account_code'] == code)
        adj_credit = sum(float(j.get('credit', 0)) for j in adjustment_journals if j['account_code'] == code)
        
        # Saldo setelah penyesuaian
        if normal_balance == 'debit':
            adjusted_balance = balance_before + adj_debit - adj_credit
            debit = adjusted_balance if adjusted_balance > 0 else 0
            credit = abs(adjusted_balance) if adjusted_balance < 0 else 0
        else:
            adjusted_balance = balance_before + adj_credit - adj_debit
            credit = adjusted_balance if adjusted_balance > 0 else 0
            debit = abs(adjusted_balance) if adjusted_balance < 0 else 0
        
        if abs(debit) > 0.01 or abs(credit) > 0.01:
            trial_balance.append({
                'account_code': code,
                'account_name': name,
                'debit': debit,
                'credit': credit
            })
    
    total_debit = sum(tb['debit'] for tb in trial_balance)
    total_credit = sum(tb['credit'] for tb in trial_balance)
    is_balanced = abs(total_debit - total_credit) < 0.01
    
    tb_html = ""
    for tb in trial_balance:
        tb_html += f"""
        <tr>
            <td class="text-center">{tb['account_code']}</td>
            <td>{tb['account_name']}</td>
            <td class="text-right">{format_rupiah(tb['debit'])}</td>
            <td class="text-right">{format_rupiah(tb['credit'])}</td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Neraca Saldo Setelah Penyesuaian - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            {generate_sidebar('akuntan', username, 'adjusted-trial')}
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Neraca Saldo Setelah Penyesuaian</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <div style="text-align: center; margin-bottom: 30px;">
                        <h2 style="color: #667eea; margin-bottom: 5px;">GEBOY MUJAIR</h2>
                        <h3 style="color: #333; margin-bottom: 5px;">NERACA SALDO SETELAH PENYESUAIAN</h3>
                        <p style="color: #666;">Per {datetime.now().strftime('%d %B %Y')}</p>
                    </div>
                    
                    <table>
                        <thead>
                            <tr>
                                <th class="text-center">Kode</th>
                                <th>Nama Akun</th>
                                <th class="text-right">Debit</th>
                                <th class="text-right">Kredit</th>
                            </tr>
                        </thead>
                        <tbody>
                            {tb_html if tb_html else '<tr><td colspan="4" class="text-center">Tidak ada data</td></tr>'}
                        </tbody>
                        {f'''<tfoot style="background: {'#d4edda' if is_balanced else '#f8d7da'}; font-weight: bold;">
                            <tr>
                                <td colspan="2" class="text-right" style="padding: 15px;">TOTAL:</td>
                                <td class="text-right" style="padding: 15px; color: #667eea;">{format_rupiah(total_debit)}</td>
                                <td class="text-right" style="padding: 15px; color: #dc3545;">{format_rupiah(total_credit)}</td>
                            </tr>
                            <tr>
                                <td colspan="4" class="text-center" style="padding: 15px; font-size: 16px;">
                                    {'✅ BALANCE' if is_balanced else '❌ NOT BALANCE'}
                                </td>
                            </tr>
                        </tfoot>''' if trial_balance else ''}
                    </table>
                </div>
                
                <div class="content-section no-print">
                    <button onclick="window.print()" class="btn-sm btn-primary btn-block">🖨️ Cetak Laporan</button>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html

@app.route('/akuntan/post-closing-trial-balance')
def akuntan_post_closing_trial_balance():
    """Neraca Saldo Setelah Penutupan"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # ✅ CEK APAKAH JURNAL PENUTUP SUDAH DIBUAT
    closing_journals = get_journal_entries(journal_type='CJ')
    
    if not closing_journals:
        flash('⚠️ Jurnal penutup belum dibuat! Silakan buat jurnal penutup terlebih dahulu.', 'error')
        return redirect(url_for('akuntan_closing_journal'))
    
    # Ambil semua akun KECUALI akun nominal (4, 5, 6)
    accounts = get_all_accounts()
    trial_balance = []
    
    for account in accounts:
        code = account['account_code']
        
        # Skip akun nominal (sudah ditutup)
        if code.startswith('4-') or code.startswith('5-') or code.startswith('6-'):
            continue
        
        # Skip Ikhtisar Laba Rugi (sudah ditutup ke modal)
        if code == '3-9901':
            continue
        
        balance = get_ledger_balance(code)
        
        if abs(balance) > 0.01:
            if account['normal_balance'] == 'debit':
                debit = balance if balance > 0 else 0
                credit = abs(balance) if balance < 0 else 0
            else:
                credit = balance if balance > 0 else 0
                debit = abs(balance) if balance < 0 else 0
            
            trial_balance.append({
                'account_code': code,
                'account_name': account['account_name'],
                'debit': debit,
                'credit': credit
            })
    
    total_debit = sum(tb['debit'] for tb in trial_balance)
    total_credit = sum(tb['credit'] for tb in trial_balance)
    is_balanced = abs(total_debit - total_credit) < 0.01
    
    # Generate HTML (sama seperti sebelumnya)
    # ...
    
    tb_html = ""
    for tb in trial_balance:
        tb_html += f"""
        <tr>
            <td class="text-center">{tb['account_code']}</td>
            <td>{tb['account_name']}</td>
            <td class="text-right">{format_rupiah(tb['debit'])}</td>
            <td class="text-right">{format_rupiah(tb['credit'])}</td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Neraca Saldo Setelah Penutupan - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            {generate_sidebar('akuntan', username, 'post-closing')}
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Neraca Saldo Setelah Penutupan</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section" style="background: #d1ecf1; border-left: 4px solid #17a2b8;">
                    <h3 style="color: #0c5460; margin-bottom: 10px;">ℹ️ Informasi</h3>
                    <p style="color: #0c5460; line-height: 1.8;">
                        Neraca Saldo Setelah Penutupan hanya menampilkan <strong>akun riil</strong> (Aset, Kewajiban, Ekuitas).<br>
                        Semua akun nominal (Pendapatan, Beban) sudah ditutup dan saldonya menjadi nol.
                    </p>
                </div>
                
                <div class="content-section">
                    <div style="text-align: center; margin-bottom: 30px;">
                        <h2 style="color: #667eea; margin-bottom: 5px;">GEBOY MUJAIR</h2>
                        <h3 style="color: #333; margin-bottom: 5px;">NERACA SALDO SETELAH PENUTUPAN</h3>
                        <p style="color: #666;">Per {datetime.now().strftime('%d %B %Y')}</p>
                    </div>
                    
                    <table>
                        <thead>
                            <tr>
                                <th class="text-center">Kode</th>
                                <th>Nama Akun</th>
                                <th class="text-right">Debit</th>
                                <th class="text-right">Kredit</th>
                            </tr>
                        </thead>
                        <tbody>
                            {tb_html if tb_html else '<tr><td colspan="4" class="text-center">Tidak ada data</td></tr>'}
                        </tbody>
                        {f'''<tfoot style="background: {'#d4edda' if is_balanced else '#f8d7da'}; font-weight: bold;">
                            <tr>
                                <td colspan="2" class="text-right" style="padding: 15px;">TOTAL:</td>
                                <td class="text-right" style="padding: 15px; color: #667eea;">{format_rupiah(total_debit)}</td>
                                <td class="text-right" style="padding: 15px; color: #dc3545;">{format_rupiah(total_credit)}</td>
                            </tr>
                            <tr>
                                <td colspan="4" class="text-center" style="padding: 15px; font-size: 16px;">
                                    {'✅ BALANCE - Siap Periode Baru' if is_balanced else '❌ NOT BALANCE'}
                                </td>
                            </tr>
                        </tfoot>''' if trial_balance else ''}
                    </table>
                </div>
                
                <div class="content-section no-print">
                    <button onclick="window.print()" class="btn-sm btn-primary btn-block">🖨️ Cetak Laporan</button>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html

@app.route('/akuntan/worksheet')
def akuntan_worksheet():
    """Neraca Lajur (Worksheet) - 10 Kolom"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # 1. AMBIL SEMUA AKUN
    accounts = get_all_accounts()
    
    # 2. AMBIL JURNAL PENYESUAIAN (AJ)
    adjustment_journals = get_journal_entries(journal_type='AJ')
    
    # 3. BUAT WORKSHEET DATA
    worksheet_data = []
    
    for account in accounts:
        code = account['account_code']
        name = account['account_name']
        normal_balance = account['normal_balance']
        
        # A. NERACA SALDO SEBELUM PENYESUAIAN
        balance_before = get_ledger_balance(code)
        
        if normal_balance == 'debit':
            ns_debet = balance_before if balance_before > 0 else 0
            ns_kredit = abs(balance_before) if balance_before < 0 else 0
        else:
            ns_kredit = balance_before if balance_before > 0 else 0
            ns_debet = abs(balance_before) if balance_before < 0 else 0
        
        # B. PENYESUAIAN (dari Jurnal Penyesuaian)
        adj_debet = 0
        adj_kredit = 0
        
        for j in adjustment_journals:
            if j['account_code'] == code:
                adj_debet += float(j.get('debit', 0))
                adj_kredit += float(j.get('credit', 0))
        
        # C. NERACA SALDO SETELAH PENYESUAIAN
        if normal_balance == 'debit':
            nsa_balance = ns_debet - ns_kredit + adj_debet - adj_kredit
            nsa_debet = nsa_balance if nsa_balance > 0 else 0
            nsa_kredit = abs(nsa_balance) if nsa_balance < 0 else 0
        else:
            nsa_balance = ns_kredit - ns_debet + adj_kredit - adj_debet
            nsa_kredit = nsa_balance if nsa_balance > 0 else 0
            nsa_debet = abs(nsa_balance) if nsa_balance < 0 else 0
        
        # D. KLASIFIKASI KE LABA RUGI atau NERACA
        lr_debet = 0
        lr_kredit = 0
        neraca_debet = 0
        neraca_kredit = 0
        
        # Akun Nominal (4, 5, 6) -> Laba Rugi
        if code.startswith('4-') or code.startswith('5-') or code.startswith('6-'):
            lr_debet = nsa_debet
            lr_kredit = nsa_kredit
        # Akun Riil (1, 2, 3) -> Neraca
        else:
            neraca_debet = nsa_debet
            neraca_kredit = nsa_kredit
        
        # Simpan jika ada saldo
        if (ns_debet + ns_kredit + adj_debet + adj_kredit + 
            nsa_debet + nsa_kredit + lr_debet + lr_kredit + 
            neraca_debet + neraca_kredit) > 0:
            
            worksheet_data.append({
                'code': code,
                'name': name,
                'ns_debet': ns_debet,
                'ns_kredit': ns_kredit,
                'adj_debet': adj_debet,
                'adj_kredit': adj_kredit,
                'nsa_debet': nsa_debet,
                'nsa_kredit': nsa_kredit,
                'lr_debet': lr_debet,
                'lr_kredit': lr_kredit,
                'neraca_debet': neraca_debet,
                'neraca_kredit': neraca_kredit
            })
    
    # 4. HITUNG TOTAL
    total_ns_debet = sum(w['ns_debet'] for w in worksheet_data)
    total_ns_kredit = sum(w['ns_kredit'] for w in worksheet_data)
    total_adj_debet = sum(w['adj_debet'] for w in worksheet_data)
    total_adj_kredit = sum(w['adj_kredit'] for w in worksheet_data)
    total_nsa_debet = sum(w['nsa_debet'] for w in worksheet_data)
    total_nsa_kredit = sum(w['nsa_kredit'] for w in worksheet_data)
    total_lr_debet = sum(w['lr_debet'] for w in worksheet_data)
    total_lr_kredit = sum(w['lr_kredit'] for w in worksheet_data)
    total_neraca_debet = sum(w['neraca_debet'] for w in worksheet_data)
    total_neraca_kredit = sum(w['neraca_kredit'] for w in worksheet_data)
    
    # 5. HITUNG LABA/RUGI
    net_income = total_lr_kredit - total_lr_debet
    
    # 6. GENERATE HTML TABLE
    worksheet_html = ""
    for w in worksheet_data:
        worksheet_html += f"""
        <tr>
            <td class="text-center"><strong>{w['code']}</strong></td>
            <td>{w['name']}</td>
            <td class="text-right">{format_rupiah(w['ns_debet']) if w['ns_debet'] > 0 else ''}</td>
            <td class="text-right">{format_rupiah(w['ns_kredit']) if w['ns_kredit'] > 0 else ''}</td>
            <td class="text-right">{format_rupiah(w['adj_debet']) if w['adj_debet'] > 0 else ''}</td>
            <td class="text-right">{format_rupiah(w['adj_kredit']) if w['adj_kredit'] > 0 else ''}</td>
            <td class="text-right">{format_rupiah(w['nsa_debet']) if w['nsa_debet'] > 0 else ''}</td>
            <td class="text-right">{format_rupiah(w['nsa_kredit']) if w['nsa_kredit'] > 0 else ''}</td>
            <td class="text-right">{format_rupiah(w['lr_debet']) if w['lr_debet'] > 0 else ''}</td>
            <td class="text-right">{format_rupiah(w['lr_kredit']) if w['lr_kredit'] > 0 else ''}</td>
            <td class="text-right">{format_rupiah(w['neraca_debet']) if w['neraca_debet'] > 0 else ''}</td>
            <td class="text-right">{format_rupiah(w['neraca_kredit']) if w['neraca_kredit'] > 0 else ''}</td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Neraca Lajur - Geboy Mujair</title>
        {generate_dashboard_style()}
        <style>
            table {{ font-size: 11px; }}
            th, td {{ padding: 8px 5px; }}
            .text-right {{ text-align: right; }}
            .text-center {{ text-align: center; }}
            @media print {{
                .no-print {{ display: none; }}
                table {{ font-size: 9px; }}
            }}
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">📊</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Akuntan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/akuntan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/akuntan/accounts"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/manual-transaction"><span class="icon">➕</span> Transaksi Manual</a></li>
                    <li><a href="/akuntan/inventory-card"><span class="icon">📦</span> Inventory Card</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Penutupan</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Pembalikan</a></li>
                    <li><a href="/akuntan/assets"><span class="icon">🏢</span> Aset</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> NS</a></li>
                    <li><a href="/akuntan/adjusted-trial-balance"><span class="icon">✅</span> NS Penyesuaian</a></li>
                    <li><a href="/akuntan/worksheet"><span class="icon">📊</span> Neraca Lajur</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">💼</span> Lap. Keuangan</a></li>
                    <li><a href="/akuntan/cash-flow-statement"><span class="icon">💰</span> Arus Kas</a></li>
                    <li><a href="/akuntan/post-closing-trial-balance"><span class="icon">🔐</span> NS Penutupan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Neraca Lajur (Worksheet)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <div style="text-align: center; margin-bottom: 20px;">
                        <h2 style="color: #667eea; margin-bottom: 5px;">GEBOY MUJAIR</h2>
                        <h3 style="color: #333; margin-bottom: 5px;">NERACA LAJUR</h3>
                        <p style="color: #666;">Per {datetime.now().strftime('%d %B %Y')}</p>
                    </div>
                    
                    <div style="overflow-x: auto;">
                        <table style="width: 100%; min-width: 1400px;">
                            <thead>
                                <tr style="background: #667eea; color: white;">
                                    <th rowspan="2" class="text-center">Kode<br>Akun</th>
                                    <th rowspan="2">Nama Akun</th>
                                    <th colspan="2" class="text-center">Daftar Saldo Sebelum<br>Penyesuaian</th>
                                    <th colspan="2" class="text-center">Penyesuaian</th>
                                    <th colspan="2" class="text-center">Daftar Saldo Setelah<br>Penyesuaian</th>
                                    <th colspan="2" class="text-center">Laporan Laba Rugi</th>
                                    <th colspan="2" class="text-center">Laporan Posisi<br>Keuangan</th>
                                </tr>
                                <tr style="background: #667eea; color: white;">
                                    <th class="text-center">Debet</th>
                                    <th class="text-center">Kredit</th>
                                    <th class="text-center">Debet</th>
                                    <th class="text-center">Kredit</th>
                                    <th class="text-center">Debet</th>
                                    <th class="text-center">Kredit</th>
                                    <th class="text-center">Debet</th>
                                    <th class="text-center">Kredit</th>
                                    <th class="text-center">Debet</th>
                                    <th class="text-center">Kredit</th>
                                </tr>
                            </thead>
                            <tbody>
                                {worksheet_html}
                                
                                <!-- TOTAL -->
                                <tr style="background: #f8f9fa; font-weight: bold;">
                                    <td colspan="2" class="text-center">TOTAL</td>
                                    <td class="text-right">{format_rupiah(total_ns_debet)}</td>
                                    <td class="text-right">{format_rupiah(total_ns_kredit)}</td>
                                    <td class="text-right">{format_rupiah(total_adj_debet)}</td>
                                    <td class="text-right">{format_rupiah(total_adj_kredit)}</td>
                                    <td class="text-right">{format_rupiah(total_nsa_debet)}</td>
                                    <td class="text-right">{format_rupiah(total_nsa_kredit)}</td>
                                    <td class="text-right">{format_rupiah(total_lr_debet)}</td>
                                    <td class="text-right">{format_rupiah(total_lr_kredit)}</td>
                                    <td class="text-right">{format_rupiah(total_neraca_debet)}</td>
                                    <td class="text-right">{format_rupiah(total_neraca_kredit)}</td>
                                </tr>
                                
                                <!-- LABA/RUGI BERSIH -->
                                {f'''
                                <tr style="background: {'#d4edda' if net_income >= 0 else '#f8d7da'}; font-weight: bold;">
                                    <td colspan="2" class="text-center">{'LABA BERSIH' if net_income >= 0 else 'RUGI BERSIH'}</td>
                                    <td colspan="6"></td>
                                    <td class="text-right">{format_rupiah(abs(net_income)) if net_income < 0 else ''}</td>
                                    <td class="text-right">{format_rupiah(net_income) if net_income >= 0 else ''}</td>
                                    <td class="text-right">{format_rupiah(net_income) if net_income >= 0 else ''}</td>
                                    <td class="text-right">{format_rupiah(abs(net_income)) if net_income < 0 else ''}</td>
                                </tr>
                                
                                <!-- TOTAL AKHIR -->
                                <tr style="background: #667eea; color: white; font-weight: bold;">
                                    <td colspan="2" class="text-center">TOTAL AKHIR</td>
                                    <td class="text-right">{format_rupiah(total_ns_debet)}</td>
                                    <td class="text-right">{format_rupiah(total_ns_kredit)}</td>
                                    <td class="text-right">{format_rupiah(total_adj_debet)}</td>
                                    <td class="text-right">{format_rupiah(total_adj_kredit)}</td>
                                    <td class="text-right">{format_rupiah(total_nsa_debet)}</td>
                                    <td class="text-right">{format_rupiah(total_nsa_kredit)}</td>
                                    <td class="text-right">{format_rupiah(total_lr_debet + (abs(net_income) if net_income < 0 else 0))}</td>
                                    <td class="text-right">{format_rupiah(total_lr_kredit + (net_income if net_income >= 0 else 0))}</td>
                                    <td class="text-right">{format_rupiah(total_neraca_debet + (net_income if net_income >= 0 else 0))}</td>
                                    <td class="text-right">{format_rupiah(total_neraca_kredit + (abs(net_income) if net_income < 0 else 0))}</td>
                                </tr>
                                ''' if net_income != 0 else ''}
                            </tbody>
                        </table>
                    </div>
                </div>
                
                <div class="content-section no-print">
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px;">
                        <button onclick="window.print()" class="btn-sm btn-primary btn-block">🖨️ Cetak Neraca Lajur</button>
                        <a href="/akuntan/financial-statements" class="btn-sm btn-success btn-block">📊 Lihat Laporan Keuangan</a>
                    </div>
                </div>
                
                <div class="content-section" style="background: #d1ecf1; border-left: 4px solid #17a2b8;">
                    <h3 style="color: #0c5460; margin-bottom: 15px;">ℹ️ Penjelasan Neraca Lajur</h3>
                    <ul style="line-height: 1.8; color: #0c5460; margin-left: 20px;">
                        <li><strong>Daftar Saldo Sebelum Penyesuaian:</strong> Saldo akun dari Neraca Saldo</li>
                        <li><strong>Penyesuaian:</strong> Entry dari Jurnal Penyesuaian (AJ)</li>
                        <li><strong>Daftar Saldo Setelah Penyesuaian:</strong> Hasil penjumlahan kolom 1 dan 2</li>
                        <li><strong>Laporan Laba Rugi:</strong> Akun nominal (Pendapatan 4-xxxx, Beban 5/6-xxxx)</li>
                        <li><strong>Laporan Posisi Keuangan:</strong> Akun riil (Aset 1-xxxx, Kewajiban 2-xxxx, Ekuitas 3-xxxx)</li>
                        <li><strong>Laba/Rugi Bersih:</strong> Selisih total Kredit - Debit di kolom Laba Rugi</li>
                    </ul>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html

# GANTI fungsi generate_financial_statements() yang lama dengan ini:

@app.route('/akuntan/financial-statements')
def akuntan_financial_statements():
    """Laporan Keuangan - 3 Laporan (dari Neraca Lajur)"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # ========== AMBIL DATA DARI NERACA LAJUR ==========
    accounts = get_all_accounts()
    adjustment_journals = get_journal_entries(journal_type='AJ')
    
    # Hitung Neraca Saldo Setelah Penyesuaian untuk setiap akun
    worksheet_data = {}
    
    for account in accounts:
        code = account['account_code']
        name = account['account_name']
        normal_balance = account['normal_balance']
        
        # Saldo sebelum penyesuaian
        balance_before = get_ledger_balance(code)
        
        # Penyesuaian
        adj_debet = sum(float(j.get('debit', 0)) for j in adjustment_journals if j['account_code'] == code)
        adj_kredit = sum(float(j.get('credit', 0)) for j in adjustment_journals if j['account_code'] == code)
        
        # Saldo setelah penyesuaian
        if normal_balance == 'debit':
            nsa_balance = balance_before + adj_debet - adj_kredit
        else:
            nsa_balance = balance_before + adj_kredit - adj_debet
        
        if abs(nsa_balance) > 0.01:  # Hanya simpan yang punya saldo
            worksheet_data[code] = {
                'name': name,
                'balance': nsa_balance,
                'normal_balance': normal_balance
            }
    
    # ========== 1. LAPORAN LABA RUGI ==========
    revenue_items = []
    expense_items = []
    
    for code, data in worksheet_data.items():
        if code.startswith('4-'):  # Pendapatan
            revenue_items.append({'code': code, 'name': data['name'], 'amount': data['balance']})
        elif code.startswith('5-') or code.startswith('6-'):  # Beban
            expense_items.append({'code': code, 'name': data['name'], 'amount': data['balance']})
    
    total_revenue = sum(item['amount'] for item in revenue_items)
    total_expense = sum(item['amount'] for item in expense_items)
    net_income = total_revenue - total_expense
    
    # HTML Laporan Laba Rugi
    revenue_html = ""
    for item in revenue_items:
        if item['amount'] > 0:
            revenue_html += f"""
            <tr>
                <td style="padding-left: 30px;">{item['name']}</td>
                <td class="text-right">{format_rupiah(item['amount'])}</td>
            </tr>
            """
    
    expense_html = ""
    for item in expense_items:
        if item['amount'] > 0:
            expense_html += f"""
            <tr>
                <td style="padding-left: 30px;">{item['name']}</td>
                <td class="text-right">{format_rupiah(item['amount'])}</td>
            </tr>
            """
    
    # ========== 2. LAPORAN PERUBAHAN EKUITAS ==========
    # Modal Awal
    modal_awal = worksheet_data.get('3-1000', {}).get('balance', 0)
    
    # Prive
    prive = worksheet_data.get('3-1100', {}).get('balance', 0)
    
    # Modal Akhir = Modal Awal + Laba Bersih - Prive
    modal_akhir = modal_awal + net_income - prive
    
    # ========== 3. LAPORAN POSISI KEUANGAN (NERACA) ==========
    asset_items = []
    liability_items = []
    equity_items = []
    
    for code, data in worksheet_data.items():
        if code.startswith('1-'):  # Aset
            asset_items.append({'code': code, 'name': data['name'], 'amount': data['balance']})
        elif code.startswith('2-'):  # Kewajiban
            liability_items.append({'code': code, 'name': data['name'], 'amount': data['balance']})
        elif code.startswith('3-') and not code.startswith('3-99'):  # Ekuitas (kecuali Ikhtisar L/R)
            equity_items.append({'code': code, 'name': data['name'], 'amount': data['balance']})
    
    total_assets = sum(item['amount'] for item in asset_items)
    total_liabilities = sum(item['amount'] for item in liability_items)
    
    # Ekuitas = Modal Akhir (dari Laporan Perubahan Ekuitas)
    total_equity = modal_akhir
    
    # HTML Assets
    asset_html = ""
    for item in asset_items:
        if abs(item['amount']) > 0.01:
            asset_html += f"""
            <tr>
                <td style="padding-left: 30px;">{item['name']}</td>
                <td class="text-right">{format_rupiah(item['amount'])}</td>
            </tr>
            """
    
    # HTML Liabilities
    liability_html = ""
    for item in liability_items:
        if abs(item['amount']) > 0.01:
            liability_html += f"""
            <tr>
                <td style="padding-left: 30px;">{item['name']}</td>
                <td class="text-right">{format_rupiah(item['amount'])}</td>
            </tr>
            """
    
    # ========== GENERATE HTML ==========
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Laporan Keuangan - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">📊</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Akuntan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/akuntan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/akuntan/accounts"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/manual-transaction"><span class="icon">➕</span> Transaksi Manual</a></li>
                    <li><a href="/akuntan/inventory-card"><span class="icon">📦</span> Inventory Card</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Penutupan</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Pembalikan</a></li>
                    <li><a href="/akuntan/assets"><span class="icon">🏢</span> Aset</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> NS</a></li>
                    <li><a href="/akuntan/adjusted-trial-balance"><span class="icon">✅</span> NS Penyesuaian</a></li>
                    <li><a href="/akuntan/worksheet"><span class="icon">📊</span> Neraca Lajur</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">💼</span> Lap. Keuangan</a></li>
                    <li><a href="/akuntan/cash-flow-statement"><span class="icon">💰</span> Arus Kas</a></li>
                    <li><a href="/akuntan/post-closing-trial-balance"><span class="icon">🔐</span> NS Penutupan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Laporan Keuangan Lengkap</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <!-- ========== 1. LAPORAN LABA RUGI ========== -->
                <div class="content-section">
                    <div style="text-align: center; margin-bottom: 30px;">
                        <h2 style="color: #667eea; margin-bottom: 5px;">GEBOY MUJAIR</h2>
                        <h3 style="color: #333; margin-bottom: 5px;">LAPORAN LABA RUGI</h3>
                        <p style="color: #666;">Untuk Periode {datetime.now().strftime('%B %Y')}</p>
                    </div>
                    
                    <table>
                        <tbody>
                            <tr style="background: #667eea; color: white;">
                                <td colspan="2" style="padding: 12px; font-weight: bold;">PENDAPATAN</td>
                            </tr>
                            {revenue_html if revenue_html else '<tr><td colspan="2" style="padding-left: 30px; color: #999;">Tidak ada pendapatan</td></tr>'}
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding-left: 30px;">Total Pendapatan</td>
                                <td class="text-right">{format_rupiah(total_revenue)}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <tr style="background: #667eea; color: white;">
                                <td colspan="2" style="padding: 12px; font-weight: bold;">BEBAN</td>
                            </tr>
                            {expense_html if expense_html else '<tr><td colspan="2" style="padding-left: 30px; color: #999;">Tidak ada beban</td></tr>'}
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding-left: 30px;">Total Beban</td>
                                <td class="text-right">{format_rupiah(total_expense)}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <tr style="background: {'#d4edda' if net_income >= 0 else '#f8d7da'}; font-weight: bold; font-size: 18px;">
                                <td style="padding: 15px;">LABA (RUGI) BERSIH</td>
                                <td class="text-right" style="padding: 15px; color: {'#155724' if net_income >= 0 else '#721c24'};">
                                    {format_rupiah(net_income)}
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                
                <!-- ========== 2. LAPORAN PERUBAHAN EKUITAS ========== -->
                <div class="content-section">
                    <div style="text-align: center; margin-bottom: 30px;">
                        <h2 style="color: #667eea; margin-bottom: 5px;">GEBOY MUJAIR</h2>
                        <h3 style="color: #333; margin-bottom: 5px;">LAPORAN PERUBAHAN EKUITAS</h3>
                        <p style="color: #666;">Untuk Periode {datetime.now().strftime('%B %Y')}</p>
                    </div>
                    
                    <table>
                        <tbody>
                            <tr>
                                <td style="padding: 12px;">Modal Awal</td>
                                <td class="text-right" style="padding: 12px;">{format_rupiah(modal_awal)}</td>
                            </tr>
                            <tr style="background: #f8f9fa;">
                                <td style="padding: 12px; padding-left: 30px;">Laba (Rugi) Bersih</td>
                                <td class="text-right" style="padding: 12px;">{format_rupiah(net_income)}</td>
                            </tr>
                            <tr>
                                <td style="padding: 12px; padding-left: 30px;">Prive</td>
                                <td class="text-right" style="padding: 12px;">({format_rupiah(prive)})</td>
                            </tr>
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding: 12px;">Penambahan Modal</td>
                                <td class="text-right" style="padding: 12px;">{format_rupiah(net_income - prive)}</td>
                            </tr>
                            <tr style="height: 10px;"><td colspan="2" style="border-bottom: 2px solid #333;"></td></tr>
                            <tr style="background: #667eea; color: white; font-weight: bold; font-size: 18px;">
                                <td style="padding: 15px;">MODAL AKHIR</td>
                                <td class="text-right" style="padding: 15px;">{format_rupiah(modal_akhir)}</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                
                <!-- ========== 3. LAPORAN POSISI KEUANGAN (NERACA) ========== -->
                <div class="content-section">
                    <div style="text-align: center; margin-bottom: 30px;">
                        <h2 style="color: #667eea; margin-bottom: 5px;">GEBOY MUJAIR</h2>
                        <h3 style="color: #333; margin-bottom: 5px;">LAPORAN POSISI KEUANGAN (NERACA)</h3>
                        <p style="color: #666;">Per {datetime.now().strftime('%d %B %Y')}</p>
                    </div>
                    
                    <table>
                        <tbody>
                            <tr style="background: #667eea; color: white;">
                                <td colspan="2" style="padding: 12px; font-weight: bold;">ASET</td>
                            </tr>
                            {asset_html if asset_html else '<tr><td colspan="2" style="padding-left: 30px; color: #999;">Tidak ada aset</td></tr>'}
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding-left: 30px;">Total Aset</td>
                                <td class="text-right">{format_rupiah(total_assets)}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <tr style="background: #667eea; color: white;">
                                <td colspan="2" style="padding: 12px; font-weight: bold;">KEWAJIBAN</td>
                            </tr>
                            {liability_html if liability_html else '<tr><td colspan="2" style="padding-left: 30px; color: #999;">Tidak ada kewajiban</td></tr>'}
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding-left: 30px;">Total Kewajiban</td>
                                <td class="text-right">{format_rupiah(total_liabilities)}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <tr style="background: #667eea; color: white;">
                                <td colspan="2" style="padding: 12px; font-weight: bold;">EKUITAS</td>
                            </tr>
                            <tr>
                                <td style="padding-left: 30px;">Modal (dari Laporan Perubahan Ekuitas)</td>
                                <td class="text-right">{format_rupiah(modal_akhir)}</td>
                            </tr>
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding-left: 30px;">Total Ekuitas</td>
                                <td class="text-right">{format_rupiah(total_equity)}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <tr style="background: #667eea; color: white; font-weight: bold; font-size: 18px;">
                                <td style="padding: 15px;">TOTAL KEWAJIBAN & EKUITAS</td>
                                <td class="text-right" style="padding: 15px;">
                                    {format_rupiah(total_liabilities + total_equity)}
                                </td>
                            </tr>
                            
                            <tr style="background: {'#d4edda' if abs(total_assets - (total_liabilities + total_equity)) < 1 else '#f8d7da'};">
                                <td colspan="2" class="text-center" style="padding: 12px; font-weight: bold;">
                                    {'✅ BALANCE - Aset = Kewajiban + Ekuitas' if abs(total_assets - (total_liabilities + total_equity)) < 1 else '❌ NOT BALANCE'}
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                
                <div class="content-section no-print">
                    <button onclick="window.print()" class="btn-sm btn-primary btn-block">🖨️ Cetak Semua Laporan Keuangan</button>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html

@app.route('/akuntan/cash-flow-statement')
def akuntan_cash_flow_statement():
    """Laporan Arus Kas"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Filter periode
    end_date = request.args.get('end_date', datetime.now().strftime('%Y-%m-%d'))
    start_date = request.args.get('start_date', datetime.now().replace(day=1).strftime('%Y-%m-%d'))
    
    cash_flow = generate_cash_flow_statement(start_date, end_date)
    
    if not cash_flow:
        flash('Gagal generate laporan arus kas', 'error')
        return redirect(url_for('dashboard_akuntan'))
    
    # Generate HTML untuk detail
    def generate_detail_html(details):
        html = ""
        for detail in details:
            html += f"""
            <tr>
                <td style="padding-left: 30px;">{detail['description']}</td>
                <td class="text-right">{format_rupiah(detail['amount'])}</td>
            </tr>
            """
        return html
    
    operating_html = generate_detail_html(cash_flow['operating']['details'])
    investing_html = generate_detail_html(cash_flow['investing']['details'])
    financing_html = generate_detail_html(cash_flow['financing']['details'])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Laporan Arus Kas - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            {generate_sidebar('akuntan', username, 'cash-flow')}
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Laporan Arus Kas</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <!-- FILTER PERIODE -->
                <div class="content-section">
                    <h2>🔍 Filter Periode</h2>
                    <form method="GET">
                        <div class="form-row">
                            <div class="form-group">
                                <label>Dari Tanggal</label>
                                <input type="date" name="start_date" value="{start_date}">
                            </div>
                            <div class="form-group">
                                <label>Sampai Tanggal</label>
                                <input type="date" name="end_date" value="{end_date}">
                            </div>
                            <div class="form-group" style="display: flex; align-items: flex-end;">
                                <button type="submit" class="btn-sm btn-primary btn-block">🔍 Tampilkan</button>
                            </div>
                        </div>
                    </form>
                </div>
                
                <!-- LAPORAN ARUS KAS -->
                <div class="content-section">
                    <div style="text-align: center; margin-bottom: 30px;">
                        <h2 style="color: #667eea; margin-bottom: 5px;">GEBOY MUJAIR</h2>
                        <h3 style="color: #333; margin-bottom: 5px;">LAPORAN ARUS KAS</h3>
                        <p style="color: #666;">Periode {datetime.strptime(start_date, '%Y-%m-%d').strftime('%d %B %Y')} - {datetime.strptime(end_date, '%Y-%m-%d').strftime('%d %B %Y')}</p>
                    </div>
                    
                    <table>
                        <tbody>
                            <!-- AKTIVITAS OPERASIONAL -->
                            <tr style="background: #667eea; color: white;">
                                <td colspan="2" style="padding: 12px; font-weight: bold;">ARUS KAS DARI AKTIVITAS OPERASIONAL</td>
                            </tr>
                            <tr>
                                <td style="padding-left: 20px; font-weight: bold;">Arus Kas Masuk:</td>
                                <td></td>
                            </tr>
                            {operating_html if cash_flow['operating']['details'] else '<tr><td colspan="2" style="padding-left: 40px; color: #999;">Tidak ada</td></tr>'}
                            <tr style="background: #f8f9fa;">
                                <td style="padding-left: 20px;">Total Arus Kas Masuk Operasional</td>
                                <td class="text-right">{format_rupiah(cash_flow['operating']['inflow'])}</td>
                            </tr>
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding-left: 20px;">Arus Kas Bersih dari Aktivitas Operasional</td>
                                <td class="text-right" style="color: {'#28a745' if cash_flow['net_operating'] >= 0 else '#dc3545'};">{format_rupiah(cash_flow['net_operating'])}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <!-- AKTIVITAS INVESTASI -->
                            <tr style="background: #667eea; color: white;">
                                <td colspan="2" style="padding: 12px; font-weight: bold;">ARUS KAS DARI AKTIVITAS INVESTASI</td>
                            </tr>
                            {investing_html if cash_flow['investing']['details'] else '<tr><td colspan="2" style="padding-left: 30px; color: #999;">Tidak ada aktivitas investasi</td></tr>'}
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding-left: 20px;">Arus Kas Bersih dari Aktivitas Investasi</td>
                                <td class="text-right" style="color: {'#28a745' if cash_flow['net_investing'] >= 0 else '#dc3545'};">{format_rupiah(cash_flow['net_investing'])}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <!-- AKTIVITAS PENDANAAN -->
                            <tr style="background: #667eea; color: white;">
                                <td colspan="2" style="padding: 12px; font-weight: bold;">ARUS KAS DARI AKTIVITAS PENDANAAN</td>
                            </tr>
                            {financing_html if cash_flow['financing']['details'] else '<tr><td colspan="2" style="padding-left: 30px; color: #999;">Tidak ada aktivitas pendanaan</td></tr>'}
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding-left: 20px;">Arus Kas Bersih dari Aktivitas Pendanaan</td>
                                <td class="text-right" style="color: {'#28a745' if cash_flow['net_financing'] >= 0 else '#dc3545'};">{format_rupiah(cash_flow['net_financing'])}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2" style="border-top: 2px solid #333;"></td></tr>
                            
                            <!-- KENAIKAN/PENURUNAN KAS -->
                            <tr style="background: #fff3cd; font-weight: bold;">
                                <td style="padding: 15px;">KENAIKAN (PENURUNAN) KAS BERSIH</td>
                                <td class="text-right" style="padding: 15px; font-size: 16px; color: {'#28a745' if cash_flow['net_change'] >= 0 else '#dc3545'};">
                                    {format_rupiah(cash_flow['net_change'])}
                                </td>
                            </tr>
                            
                            <tr>
                                <td style="padding: 12px;">Kas Awal Periode</td>
                                <td class="text-right" style="padding: 12px;">{format_rupiah(cash_flow['beginning_cash'])}</td>
                            </tr>
                            
                            <tr style="background: #667eea; color: white; font-weight: bold; font-size: 18px;">
                                <td style="padding: 15px;">KAS AKHIR PERIODE</td>
                                <td class="text-right" style="padding: 15px;">{format_rupiah(cash_flow['ending_cash'])}</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                
                <div class="content-section no-print">
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px;">
                        <button onclick="window.print()" class="btn-sm btn-primary btn-block">🖨️ Cetak Laporan</button>
                        <button onclick="downloadPDF()" class="btn-sm btn-success btn-block">📥 Download PDF</button>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
        function downloadPDF() {{
            // Implementasi download PDF akan ditambahkan nanti
            alert('Fitur download PDF sedang dalam pengembangan');
        }}
        </script>
    </body>
    </html>
    """
    
    return html

@app.route('/owner/analytics')
def owner_analytics():
    """Analytics untuk owner"""
    if 'username' not in session or session.get('role') != 'owner':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Data untuk grafik
    transactions = get_transactions()
    
    # Sales per bulan
    sales_by_month = {}
    for trans in transactions:
        date_obj = datetime.fromisoformat(trans['date'].replace('Z', '+00:00'))
        month_key = date_obj.strftime('%Y-%m')
        sales_by_month[month_key] = sales_by_month.get(month_key, 0) + float(trans['total_amount'])
    
    months = sorted(sales_by_month.keys())[-6:]  # 6 bulan terakhir
    sales_data = [{'month': m, 'sales': sales_by_month[m]} for m in months]
    
    # Total stats
    total_revenue = sum(float(t['total_amount']) for t in transactions)
    journals = get_journal_entries()
    total_expenses = sum(float(j.get('debit', 0)) for j in journals if j['account_code'].startswith('5-') or j['account_code'].startswith('6-'))
    net_income = total_revenue - total_expenses
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Analytics - Geboy Mujair</title>
        {generate_dashboard_style()}
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">👔</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Owner</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/owner"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/owner/analytics" class="active"><span class="icon">📈</span> Analytics</a></li>
                    <li><a href="/owner/financial-reports"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/owner/users"><span class="icon">👥</span> Manajemen User</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Business Analytics</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">💵</div>
                        <div class="stat-value">{format_rupiah(total_revenue)}</div>
                        <div class="stat-label">Total Pendapatan</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">💸</div>
                        <div class="stat-value">{format_rupiah(total_expenses)}</div>
                        <div class="stat-label">Total Pengeluaran</div>
                    </div>
                    <div class="stat-card" style="background: linear-gradient(135deg, {'#28a745' if net_income >= 0 else '#dc3545'} 0%, {'#218838' if net_income >= 0 else '#c82333'} 100%);">
                        <div class="stat-icon">{'📈' if net_income >= 0 else '📉'}</div>
                        <div class="stat-value">{format_rupiah(net_income)}</div>
                        <div class="stat-label">Laba Bersih</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📝</div>
                        <div class="stat-value">{len(transactions)}</div>
                        <div class="stat-label">Total Transaksi</div>
                    </div>
                </div>
                
                <div class="content-section">
                    <h2>📈 Grafik Penjualan 6 Bulan Terakhir</h2>
                    <canvas id="salesChart" style="max-height: 400px;"></canvas>
                </div>
                
                <div class="content-section">
                    <h2>🎯 Key Performance Indicators</h2>
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px;">
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; border-left: 4px solid #667eea;">
                            <h3 style="color: #667eea; margin-bottom: 10px;">Rata-rata Transaksi</h3>
                            <p style="font-size: 24px; font-weight: bold; color: #333;">
                                {format_rupiah(total_revenue / len(transactions) if transactions else 0)}
                            </p>
                        </div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; border-left: 4px solid #28a745;">
                            <h3 style="color: #28a745; margin-bottom: 10px;">Profit Margin</h3>
                            <p style="font-size: 24px; font-weight: bold; color: #333;">
                                {f"{(net_income / total_revenue * 100):.2f}%" if total_revenue > 0 else "0%"}
                            </p>
                        </div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; border-left: 4px solid #ffc107;">
                            <h3 style="color: #ffc107; margin-bottom: 10px;">Transaksi Bulanan</h3>
                            <p style="font-size: 24px; font-weight: bold; color: #333;">
                                {len([t for t in transactions if datetime.fromisoformat(t['date'].replace('Z', '+00:00')).month == datetime.now().month])}
                            </p>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
        const ctx = document.getElementById('salesChart').getContext('2d');
        const salesData = {json.dumps(sales_data)};
        
        new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: salesData.map(d => {{
                    const [year, month] = d.month.split('-');
                    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des'];
                    return months[parseInt(month) - 1] + ' ' + year;
                }}),
                datasets: [{{
                    label: 'Penjualan (Rp)',
                    data: salesData.map(d => d.sales),
                    backgroundColor: 'rgba(102, 126, 234, 0.2)',
                    borderColor: 'rgba(102, 126, 234, 1)',
                    borderWidth: 3,
                    fill: true,
                    tension: 0.4
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: true,
                scales: {{
                    y: {{
                        beginAtZero: true,
                        ticks: {{
                            callback: function(value) {{
                                return 'Rp' + value.toLocaleString('id-ID');
                            }}
                        }}
                    }}
                }},
                plugins: {{
                    legend: {{
                        display: true,
                        position: 'top'
                    }},
                    tooltip: {{
                        callbacks: {{
                            label: function(context) {{
                                return 'Penjualan: Rp' + context.parsed.y.toLocaleString('id-ID');
                            }}
                        }}
                    }}
                }}
            }}
        }});
        </script>
    </body>
    </html>
    """
    
    return html

@app.route('/owner/financial-reports')
def owner_financial_reports():
    """Laporan keuangan lengkap untuk owner (read-only)"""
    if 'username' not in session or session.get('role') != 'owner':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Generate semua laporan
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = datetime.now().replace(day=1).strftime('%Y-%m-%d')
    
    income_statement = generate_income_statement(start_date, end_date)
    balance_sheet = generate_balance_sheet(end_date)
    cash_flow = generate_cash_flow_statement(start_date, end_date)
    
    # ... (gunakan kode yang sama dengan akuntan_financial_statements + cash_flow)
    # Tapi sidebar pakai 'owner'
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <title>Laporan Keuangan - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            {generate_sidebar('owner', username, 'financial')}
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Laporan Keuangan Lengkap</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <!-- TAB NAVIGATION -->
                <div class="content-section">
                    <div style="display: flex; gap: 10px; margin-bottom: 20px;">
                        <button onclick="showReport('laba-rugi')" class="btn-sm btn-primary">📊 Laba Rugi</button>
                        <button onclick="showReport('neraca')" class="btn-sm btn-info">⚖️ Neraca</button>
                        <button onclick="showReport('arus-kas')" class="btn-sm btn-success">💰 Arus Kas</button>
                    </div>
                </div>
                
                <!-- LAPORAN LABA RUGI -->
                <div id="laba-rugi" class="report-section">
                    <!-- Copy dari akuntan_financial_statements -->
                </div>
                
                <!-- NERACA -->
                <div id="neraca" class="report-section" style="display: none;">
                    <!-- Copy dari akuntan_financial_statements -->
                </div>
                
                <!-- ARUS KAS -->
                <div id="arus-kas" class="report-section" style="display: none;">
                    <!-- Copy dari akuntan_cash_flow_statement -->
                </div>
                
                <div class="content-section no-print">
                    <button onclick="window.print()" class="btn-sm btn-primary btn-block">🖨️ Cetak</button>
                </div>
            </div>
        </div>
        
        <script>
        function showReport(id) {{
            document.querySelectorAll('.report-section').forEach(el => el.style.display = 'none');
            document.getElementById(id).style.display = 'block';
        }}
        </script>
    </body>
    </html>
    """
    
    return html

@app.route('/owner/users')
def owner_users():
    """Manajemen user untuk owner"""
    if 'username' not in session or session.get('role') != 'owner':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Ambil semua users
    try:
        response = supabase.table('users').select('*').execute()
        users = response.data if response.data else []
    except:
        users = []
    
    users_html = ""
    role_icons = {
        'kasir': '💰',
        'akuntan': '📊',
        'owner': '👔',
        'karyawan': '👷'
    }
    
    for user in users:
        users_html += f"""
        <tr>
            <td class="text-center">{role_icons.get(user['role'], '👤')}</td>
            <td>{user['username']}</td>
            <td>{user['email']}</td>
            <td class="text-center">
                <span style="background: #667eea; color: white; padding: 5px 15px; border-radius: 20px; font-size: 12px; text-transform: capitalize;">
                    {user['role']}
                </span>
            </td>
            <td>{datetime.fromisoformat(user['created_at'].replace('Z', '+00:00')).strftime('%d/%m/%Y %H:%M') if user.get('created_at') else '-'}</td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Manajemen User - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">👔</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Owner</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/owner"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/owner/analytics"><span class="icon">📈</span> Analytics</a></li>
                    <li><a href="/owner/financial-reports"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/owner/users" class="active"><span class="icon">👥</span> Manajemen User</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Manajemen User</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="stats-grid" style="grid-template-columns: repeat(4, 1fr);">
                    <div class="stat-card">
                        <div class="stat-icon">👥</div>
                        <div class="stat-value">{len(users)}</div>
                        <div class="stat-label">Total User</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">💰</div>
                        <div class="stat-value">{len([u for u in users if u['role'] == 'kasir'])}</div>
                        <div class="stat-label">Kasir</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📊</div>
                        <div class="stat-value">{len([u for u in users if u['role'] == 'akuntan'])}</div>
                        <div class="stat-label">Akuntan</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">👷</div>
                        <div class="stat-value">{len([u for u in users if u['role'] == 'karyawan'])}</div>
                        <div class="stat-label">Karyawan</div>
                    </div>
                </div>
                
                <div class="content-section">
                    <h2>👥 Daftar User</h2>
                    <table>
                        <thead>
                            <tr>
                                <th class="text-center">Icon</th>
                                <th>Username</th>
                                <th>Email</th>
                                <th class="text-center">Role</th>
                                <th>Terdaftar</th>
                            </tr>
                        </thead>
                        <tbody>
                            {users_html if users_html else '<tr><td colspan="5" class="text-center">Tidak ada user</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html

# ============== ADDITIONAL HELPER ROUTES ==============

@app.route('/api/update-account-balance/<account_code>')
def api_update_account_balance(account_code):
    """API untuk update saldo akun (untuk AJAX)"""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    balance = get_ledger_balance(account_code)
    return jsonify({'balance': balance, 'formatted': format_rupiah(balance)})

@app.route('/api/accounts')
def api_accounts():
    """API untuk mendapatkan daftar akun"""
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    
    accounts = get_all_accounts()
    return jsonify({'accounts': accounts})

# ============== ERROR HANDLERS ==============

@app.errorhandler(404)
def not_found(e):
    return f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>404 - Halaman Tidak Ditemukan</title>
        {generate_base_style()}
    </head>
    <body> 
        <div class="container" style="text-align: center;">
            <div class="logo" style="font-size: 80px;">❌</div>
            <h1 style="color: #dc3545;">404 - Halaman Tidak Ditemukan</h1>
            <p style="color: #666; margin: 20px 0;">Maaf, halaman yang Anda cari tidak ditemukan.</p>
            <a href="/" class="btn" style="display: inline-block; text-decoration: none; margin-top: 20px;">🏠 Kembali ke Beranda</a>
        </div>
    </body>
    </html>
    """, 404

@app.errorhandler(500)
def internal_error(e):
    return f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>500 - Server Error</title>
        {generate_base_style()}
    </head>
    <body>
        <div class="container" style="text-align: center;">
            <div class="logo" style="font-size: 80px;">⚠️</div>
            <h1 style="color: #ffc107;">500 - Terjadi Kesalahan Server</h1>
            <p style="color: #666; margin: 20px 0;">Maaf, terjadi kesalahan pada server. Silakan coba lagi nanti.</p>
            <a href="/" class="btn" style="display: inline-block; text-decoration: none; margin-top: 20px;">🏠 Kembali ke Beranda</a>
        </div>
    </body>
    </html>
    """, 500

# ============== ROUTES TAMBAHAN UNTUK JURNAL PENYESUAIAN, PENUTUP, PEMBALIK ==============

@app.route('/akuntan/adjustment-journal', methods=['GET', 'POST'])
def akuntan_adjustment_journal():
    """Jurnal Penyesuaian"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            date = request.form.get('date')
            entries = []
            
            # Ambil semua entries dari form
            for i in range(10):  # Max 10 entries
                account_code = request.form.get(f'account_code_{i}')
                if account_code:
                    description = request.form.get(f'description_{i}')
                    debit = parse_rupiah(request.form.get(f'debit_{i}', '0'))
                    credit = parse_rupiah(request.form.get(f'credit_{i}', '0'))
                    
                    accounts = get_all_accounts()
                    account = next((a for a in accounts if a['account_code'] == account_code), None)
                    
                    if account:
                        entries.append({
                            'account_code': account_code,
                            'account_name': account['account_name'],
                            'description': description,
                            'debit': debit,
                            'credit': credit
                        })
            
            # Validasi balance
            total_debit = sum(e['debit'] for e in entries)
            total_credit = sum(e['credit'] for e in entries)
            
            if abs(total_debit - total_credit) > 0.01:
                flash('Jurnal tidak balance! Total Debit harus sama dengan Total Kredit.', 'error')
            else:
                ref_code = f"AJ{datetime.now().strftime('%d%m%Y')}"
                for entry in entries:
                    create_adjustment_entry(
                        date=date,
                        account_code=entry['account_code'],
                        account_name=entry['account_name'],
                        description=entry['description'],
                        debit=entry['debit'],
                        credit=entry['credit'],
                        ref_code=ref_code
                    )
                flash('Jurnal penyesuaian berhasil disimpan!', 'success')
                return redirect(url_for('akuntan_adjustment_journal'))
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
    
    username = session.get('username', 'User')
    journals = get_journal_entries(journal_type='AJ')
    accounts = get_all_accounts()
    
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    accounts_options = "".join([f'<option value="{a["account_code"]}">{a["account_code"]} - {a["account_name"]}</option>' for a in accounts])
    
    journals_html = ""
    for j in journals:
        journals_html += f"""
        <tr>
            <td>{j['date']}</td>
            <td class="text-center">{j['account_code']}</td>
            <td>{j['account_name']}</td>
            <td>{j['description']}</td>
            <td class="text-center">{j.get('ref_code', '-')}</td>
            <td class="text-right">{format_rupiah(j.get('debit', 0))}</td>
            <td class="text-right">{format_rupiah(j.get('credit', 0))}</td>
        </tr>
        """
    
    html = fr"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Jurnal Penyesuaian - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">📊</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Akuntan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/akuntan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/akuntan/accounts"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/manual-transaction"><span class="icon">➕</span> Transaksi Manual</a></li>
                    <li><a href="/akuntan/inventory-card"><span class="icon">📦</span> Inventory Card</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Penutupan</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Pembalikan</a></li>
                    <li><a href="/akuntan/assets"><span class="icon">🏢</span> Aset</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> NS</a></li>
                    <li><a href="/akuntan/adjusted-trial-balance"><span class="icon">✅</span> NS Penyesuaian</a></li>
                    <li><a href="/akuntan/worksheet"><span class="icon">📊</span> Neraca Lajur</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">💼</span> Lap. Keuangan</a></li>
                    <li><a href="/akuntan/cash-flow-statement"><span class="icon">💰</span> Arus Kas</a></li>
                    <li><a href="/akuntan/post-closing-trial-balance"><span class="icon">🔐</span> NS Penutupan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Jurnal Penyesuaian (Adjustment Journal)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <h2>➕ Buat Jurnal Penyesuaian</h2>
                    {flash_html}
                    <form method="POST" id="adjustmentForm">
                        <div class="form-group">
                            <label>Tanggal *</label>
                            <input type="date" name="date" required value="{datetime.now().strftime('%Y-%m-%d')}">
                        </div>
                        
                        <div id="entries">
                            <div class="entry-row" style="background: #f8f9fa; padding: 15px; border-radius: 10px; margin-bottom: 15px;">
                                <h4 style="margin-bottom: 15px; color: #667eea;">Entry 1</h4>
                                <div class="form-row">
                                    <div class="form-group">
                                        <label>Akun</label>
                                        <select name="account_code_0" required>
                                            <option value="">-- Pilih Akun --</option>
                                            {accounts_options}
                                        </select>
                                    </div>
                                    <div class="form-group">
                                        <label>Keterangan</label>
                                        <input type="text" name="description_0" required placeholder="Keterangan...">
                                    </div>
                                </div>
                                <div class="form-row">
                                    <div class="form-group">
                                        <label>Debit</label>
                                        <input type="text" name="debit_0" placeholder="Rp0,00" class="debit-input">
                                    </div>
                                    <div class="form-group">
                                        <label>Kredit</label>
                                        <input type="text" name="credit_0" placeholder="Rp0,00" class="credit-input">
                                    </div>
                                </div>
                            </div>
                            
                            <div class="entry-row" style="background: #f8f9fa; padding: 15px; border-radius: 10px; margin-bottom: 15px;">
                                <h4 style="margin-bottom: 15px; color: #667eea;">Entry 2</h4>
                                <div class="form-row">
                                    <div class="form-group">
                                        <label>Akun</label>
                                        <select name="account_code_1">
                                            <option value="">-- Pilih Akun --</option>
                                            {accounts_options}
                                        </select>
                                    </div>
                                    <div class="form-group">
                                        <label>Keterangan</label>
                                        <input type="text" name="description_1" placeholder="Keterangan...">
                                    </div>
                                </div>
                                <div class="form-row">
                                    <div class="form-group">
                                        <label>Debit</label>
                                        <input type="text" name="debit_1" placeholder="Rp0,00" class="debit-input">
                                    </div>
                                    <div class="form-group">
                                        <label>Kredit</label>
                                        <input type="text" name="credit_1" placeholder="Rp0,00" class="credit-input">
                                    </div>
                                </div>
                            </div>
                        </div>
                        
                        <div style="background: #667eea; color: white; padding: 15px; border-radius: 10px; margin-top: 20px; display: flex; justify-content: space-between; align-items: center;">
                            <div>
                                <strong>Total Debit:</strong> <span id="totalDebit">Rp0,00</span>
                            </div>
                            <div>
                                <strong>Total Kredit:</strong> <span id="totalCredit">Rp0,00</span>
                            </div>
                            <div>
                                <strong>Balance:</strong> <span id="balance">Rp0,00</span>
                            </div>
                        </div>
                        
                        <button type="submit" class="btn-sm btn-success btn-block" style="margin-top: 20px;">💾 Simpan Jurnal Penyesuaian</button>
                    </form>
                </div>
                
                <div class="content-section">
                    <h2>📝 Daftar Jurnal Penyesuaian</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Tanggal</th>
                                <th class="text-center">Kode</th>
                                <th>Akun</th>
                                <th>Keterangan</th>
                                <th class="text-center">Ref</th>
                                <th class="text-right">Debit</th>
                                <th class="text-right">Kredit</th>
                            </tr>
                        </thead>
                        <tbody>
                            {journals_html if journals_html else '<tr><td colspan="7" class="text-center">Belum ada jurnal penyesuaian</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
        
        <script>
        // Format rupiah untuk semua input
        document.querySelectorAll('.debit-input, .credit-input').forEach(input => {{
            input.addEventListener('blur', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = 'Rp' + parseInt(val).toLocaleString('id-ID') + ',00';
                }}
                calculateTotals();
            }});
            
            input.addEventListener('input', calculateTotals);
        }});
        
        function parseRupiah(str) {{
            if (!str) return 0;
            return parseFloat(str.replace(/Rp/g, '').replace(/\\./g, '').replace(',', '.')) || 0;
        }}
        
        function formatRupiah(num) {{
            return 'Rp' + num.toLocaleString('id-ID', {{
                minimumFractionDigits: 2,
                maximumFractionDigits: 2
            }}).replace(',', 'X').replace('.', ',').replace('X', '.');
        }}
        
        function calculateTotals() {{
            let totalDebit = 0;
            let totalCredit = 0;
            
            document.querySelectorAll('.debit-input').forEach(input => {{
                totalDebit += parseRupiah(input.value);
            }});
            
            document.querySelectorAll('.credit-input').forEach(input => {{
                totalCredit += parseRupiah(input.value);
            }});
            
            document.getElementById('totalDebit').textContent = formatRupiah(totalDebit);
            document.getElementById('totalCredit').textContent = formatRupiah(totalCredit);
            
            const balance = totalDebit - totalCredit;
            const balanceEl = document.getElementById('balance');
            balanceEl.textContent = formatRupiah(Math.abs(balance));
            balanceEl.style.color = Math.abs(balance) < 0.01 ? '#28a745' : '#dc3545';
        }}
        </script>
    </body>
    </html>
    """
    return html

@app.route('/akuntan/closing-journal', methods=['GET', 'POST'])
def akuntan_closing_journal():
    """Jurnal Penutup"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            date = request.form.get('date')
            
            # Generate jurnal penutup otomatis
            # 1. Tutup akun pendapatan ke Ikhtisar Laba Rugi
            accounts = get_all_accounts()
            revenue_accounts = [a for a in accounts if a['account_code'].startswith('4-')]
            
            for acc in revenue_accounts:
                balance = get_ledger_balance(acc['account_code'])
                if balance > 0:
                    # Debit Pendapatan
                    create_closing_entry(date, acc['account_code'], acc['account_name'], 
                                       'Penutupan Pendapatan', balance, 0)
                    # Credit Ikhtisar Laba Rugi
                    create_closing_entry(date, '3-9901', 'Ikhtisar Laba Rugi', 
                                       'Penutupan Pendapatan', 0, balance)
            
            # 2. Tutup akun beban ke Ikhtisar Laba Rugi
            expense_accounts = [a for a in accounts if a['account_code'].startswith('5-') or a['account_code'].startswith('6-')]
            
            for acc in expense_accounts:
                balance = get_ledger_balance(acc['account_code'])
                if balance > 0:
                    # Debit Ikhtisar Laba Rugi
                    create_closing_entry(date, '3-9901', 'Ikhtisar Laba Rugi', 
                                       'Penutupan Beban', balance, 0)
                    # Credit Beban
                    create_closing_entry(date, acc['account_code'], acc['account_name'], 
                                       'Penutupan Beban', 0, balance)
            
            # 3. Tutup Ikhtisar Laba Rugi ke Modal
            net_income = get_ledger_balance('3-1200')
            if net_income != 0:
                if net_income > 0:  # Laba
                    create_closing_entry(date, '3-1200', 'Ikhtisar Laba Rugi', 
                                       'Penutupan Laba ke Modal', net_income, 0)
                    create_closing_entry(date, '3-1000', 'Modal', 
                                       'Penutupan Laba ke Modal', 0, net_income)
                else:  # Rugi
                    create_closing_entry(date, '3-1000', 'Modal', 
                                       'Penutupan Rugi ke Modal', abs(net_income), 0)
                    create_closing_entry(date, '3-1200', 'Ikhtisar Laba Rugi', 
                                       'Penutupan Rugi ke Modal', 0, abs(net_income))
            
            flash('Jurnal penutup berhasil dibuat!', 'success')
            return redirect(url_for('akuntan_closing_journal'))
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
    
    username = session.get('username', 'User')
    journals = get_journal_entries(journal_type='CJ')
    
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    journals_html = ""
    for j in journals:
        journals_html += f"""
        <tr>
            <td>{j['date']}</td>
            <td class="text-center">{j['account_code']}</td>
            <td>{j['account_name']}</td>
            <td>{j['description']}</td>
            <td class="text-right">{format_rupiah(j.get('debit', 0))}</td>
            <td class="text-right">{format_rupiah(j.get('credit', 0))}</td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Jurnal Penutup - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">📊</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Akuntan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/akuntan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/akuntan/accounts"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/manual-transaction"><span class="icon">➕</span> Transaksi Manual</a></li>
                    <li><a href="/akuntan/inventory-card"><span class="icon">📦</span> Inventory Card</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Penutupan</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Pembalikan</a></li>
                    <li><a href="/akuntan/assets"><span class="icon">🏢</span> Aset</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> NS</a></li>
                    <li><a href="/akuntan/adjusted-trial-balance"><span class="icon">✅</span> NS Penyesuaian</a></li>
                    <li><a href="/akuntan/worksheet"><span class="icon">📊</span> Neraca Lajur</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">💼</span> Lap. Keuangan</a></li>
                    <li><a href="/akuntan/cash-flow-statement"><span class="icon">💰</span> Arus Kas</a></li>
                    <li><a href="/akuntan/post-closing-trial-balance"><span class="icon">🔐</span> NS Penutupan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Jurnal Penutup (Closing Journal)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section" style="background: #fff3cd; border-left: 4px solid #ffc107;">
                    <h3 style="color: #856404; margin-bottom: 15px;">⚠️ Peringatan</h3>
                    <p style="line-height: 1.8; color: #856404;">
                        Jurnal penutup hanya dibuat di <strong>akhir periode akuntansi</strong> (akhir tahun/bulan).<br>
                        Proses ini akan menutup semua akun <strong>Pendapatan</strong> dan <strong>Beban</strong> ke <strong>Ikhtisar Laba Rugi</strong>,
                        kemudian memindahkan saldo Laba/Rugi ke akun <strong>Modal</strong>.<br><br>
                        <strong>Pastikan semua transaksi sudah lengkap dan jurnal penyesuaian sudah dibuat sebelum membuat jurnal penutup.</strong>
                    </p>
                </div>
                
                <div class="content-section">
                    <h2>🔒 Buat Jurnal Penutup</h2>
                    {flash_html}
                    <form method="POST">
                        <div class="form-group">
                            <label>Tanggal Penutupan *</label>
                            <input type="date" name="date" required value="{datetime.now().strftime('%Y-%m-%d')}">
                            <small style="color: #666; display: block; margin-top: 5px;">Pilih tanggal akhir periode (biasanya 31 Desember)</small>
                        </div>
                        
                        <button type="submit" class="btn-sm btn-danger btn-block" onclick="return confirm('Yakin ingin membuat jurnal penutup? Proses ini akan menutup semua akun nominal.')">
                            🔒 Buat Jurnal Penutup Otomatis
                        </button>
                    </form>
                </div>
                
                <div class="content-section">
                    <h2>📝 Daftar Jurnal Penutup</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Tanggal</th>
                                <th class="text-center">Kode</th>
                                <th>Akun</th>
                                <th>Keterangan</th>
                                <th class="text-right">Debit</th>
                                <th class="text-right">Kredit</th>
                            </tr>
                        </thead>
                        <tbody>
                            {journals_html if journals_html else '<tr><td colspan="6" class="text-center">Belum ada jurnal penutup</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html

# ============== INISIALISASI DATABASE (JIKA BELUM ADA AKUN DEFAULT) ==============

def init_default_accounts():
    """Inisialisasi akun-akun default jika belum ada"""
    accounts = get_all_accounts()
    if len(accounts) == 0:
        default_accounts = [
            # ASET (1-xxxx)
            ('1-1000', 'Kas', 'aset', 'debit', 0),
            ('1-1400', 'Piutang Usaha', 'aset', 'debit', 0),
            ('1-1200', 'Persediaan Ikan Mujair', 'aset', 'debit', 0),
            ('1-1300', 'Perlengkapan', 'aset', 'debit', 0),
            ('1-2200', 'Peralatan', 'aset', 'debit', 0),
            ('1-2210', 'Akumulasi Penyusutan Peralatan', 'aset', 'credit', 0),
            
            # KEWAJIBAN (2-xxxx)
            ('2-1000', 'Utang Usaha', 'kewajiban', 'credit', 0),
            ('2-2000', 'Utang Bank', 'kewajiban', 'credit', 0),
            
            # EKUITAS (3-xxxx)
            ('3-1000', 'Modal', 'ekuitas', 'credit', 0),
            ('3-2000', 'Prive', 'ekuitas', 'debit', 0),
            ('3-3000', 'Ikhtisar Laba Rugi', 'ekuitas', 'credit', 0),
            
            # PENDAPATAN (4-xxxx)
            ('4-1000', 'Penjualan', 'pendapatan', 'credit', 0),
            ('4-1201', 'Pendapatan Lain-lain', 'pendapatan', 'credit', 0),
            
            # HARGA POKOK PENJUALAN (5-xxxx)
            ('5-1000', 'Harga Pokok Penjualan', 'beban', 'debit', 0),
            # BEBAN (6-xxxx)
            ('6-1300', 'Beban Gaji', 'beban', 'debit', 0),
            ('6-1000', 'Beban Telepon, Air, Listrik', 'beban', 'debit', 0),
            ('6-1100', 'Beban Perlengkapan', 'beban', 'debit', 0),
            ('6-1400', 'Beban Penyusutan Peralatan', 'beban', 'debit', 0),
            ('6-1500', 'Beban Perawatan Kolam', 'beban', 'debit', 0),
            ('6-1501', 'Beban Lain-lain', 'beban', 'debit', 0),
        ]
        
        for acc_code, acc_name, acc_type, normal_bal, beginning_bal in default_accounts:
            create_account(acc_code, acc_name, acc_type, normal_bal, beginning_bal)
        
        print("✓ Default accounts initialized!")

# Panggil saat aplikasi start
with app.app_context():
    try:
        init_default_accounts()
    except:
        pass
# ============== ROUTES JURNAL PEMBALIK ==============

@app.route('/akuntan/reversing-journal', methods=['GET', 'POST'])
def akuntan_reversing_journal():
    """Jurnal Pembalik"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            date = request.form.get('date')
            
            # Ambil jurnal penyesuaian tertentu yang perlu dibalik
            # Biasanya: beban dibayar dimuka, pendapatan diterima dimuka
            adjustment_journals = get_journal_entries(journal_type='AJ')
            
            # Balik jurnal penyesuaian
            for j in adjustment_journals:
                # Jika ada kata kunci tertentu yang perlu dibalik
                if 'dibayar dimuka' in j['description'].lower() or 'diterima dimuka' in j['description'].lower():
                    # Balik debit-kredit
                    if j['debit'] > 0:
                        create_reversing_entry(date, j['account_code'], j['account_name'], 
                                             f"Pembalikan: {j['description']}", 0, j['debit'])
                    if j['credit'] > 0:
                        create_reversing_entry(date, j['account_code'], j['account_name'], 
                                             f"Pembalikan: {j['description']}", j['credit'], 0)
            
            flash('Jurnal pembalik berhasil dibuat!', 'success')
            return redirect(url_for('akuntan_reversing_journal'))
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
    
    username = session.get('username', 'User')
    journals = get_journal_entries(journal_type='RJ')
    
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    journals_html = ""
    for j in journals:
        journals_html += f"""
        <tr>
            <td>{j['date']}</td>
            <td class="text-center">{j['account_code']}</td>
            <td>{j['account_name']}</td>
            <td>{j['description']}</td>
            <td class="text-right">{format_rupiah(j.get('debit', 0))}</td>
            <td class="text-right">{format_rupiah(j.get('credit', 0))}</td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Jurnal Pembalik - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">📊</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Akuntan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/akuntan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/akuntan/accounts"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/manual-transaction"><span class="icon">➕</span> Transaksi Manual</a></li>
                    <li><a href="/akuntan/inventory-card"><span class="icon">📦</span> Inventory Card</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Penutupan</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Pembalikan</a></li>
                    <li><a href="/akuntan/assets"><span class="icon">🏢</span> Aset</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> NS</a></li>
                    <li><a href="/akuntan/adjusted-trial-balance"><span class="icon">✅</span> NS Penyesuaian</a></li>
                    <li><a href="/akuntan/worksheet"><span class="icon">📊</span> Neraca Lajur</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">💼</span> Lap. Keuangan</a></li>
                    <li><a href="/akuntan/cash-flow-statement"><span class="icon">💰</span> Arus Kas</a></li>
                    <li><a href="/akuntan/post-closing-trial-balance"><span class="icon">🔐</span> NS Penutupan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Jurnal Pembalik (Reversing Journal)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section" style="background: #d1ecf1; border-left: 4px solid #17a2b8;">
                    <h3 style="color: #0c5460; margin-bottom: 15px;">ℹ️ Informasi</h3>
                    <p style="line-height: 1.8; color: #0c5460;">
                        Jurnal pembalik dibuat di <strong>awal periode berikutnya</strong> untuk membalik jurnal penyesuaian tertentu.<br>
                        Jurnal yang umumnya dibalik:<br>
                        • Beban yang masih harus dibayar<br>
                        • Pendapatan yang masih harus diterima<br>
                        • Beban dibayar dimuka (jika dicatat sebagai beban)<br>
                        • Pendapatan diterima dimuka (jika dicatat sebagai pendapatan)<br><br>
                        <strong>Tujuan:</strong> Mempermudah pencatatan transaksi rutin di periode berikutnya.
                    </p>
                </div>
                
                <div class="content-section">
                    <h2>🔄 Buat Jurnal Pembalik</h2>
                    {flash_html}
                    <form method="POST">
                        <div class="form-group">
                            <label>Tanggal Pembalik *</label>
                            <input type="date" name="date" required value="{(datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')}">
                            <small style="color: #666; display: block; margin-top: 5px;">Pilih tanggal awal periode baru (biasanya 1 Januari)</small>
                        </div>
                        
                        <button type="submit" class="btn-sm btn-info btn-block">
                            🔄 Buat Jurnal Pembalik Otomatis
                        </button>
                    </form>
                </div>
                
                <div class="content-section">
                    <h2>📝 Daftar Jurnal Pembalik</h2>
                    <table>
                        <thead>
                            <tr>
                                <th>Tanggal</th>
                                <th class="text-center">Kode</th>
                                <th>Akun</th>
                                <th>Keterangan</th>
                                <th class="text-right">Debit</th>
                                <th class="text-right">Kredit</th>
                            </tr>
                        </thead>
                        <tbody>
                            {journals_html if journals_html else '<tr><td colspan="6" class="text-center">Belum ada jurnal pembalik</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html

# ============== ROUTES TAMBAHAN UNTUK ASET ==============
@app.route('/akuntan/assets', methods=['GET', 'POST'])
def akuntan_assets():
    """Kelola Aset Tetap & Penyusutan"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        action = request.form.get('action')
        
        if action == 'add_asset':
            try:
                asset_name = request.form.get('asset_name')
                asset_code = request.form.get('asset_code')
                cost = parse_rupiah(request.form.get('cost'))
                salvage_value = parse_rupiah(request.form.get('salvage_value', '0'))
                useful_life = int(request.form.get('useful_life'))
                method = request.form.get('method')
                purchase_date = request.form.get('purchase_date')
                
                if create_asset(asset_name, asset_code, cost, salvage_value, useful_life, method, purchase_date):
                    flash('✅ Aset berhasil ditambahkan!', 'success')
                else:
                    flash('❌ Gagal menambahkan aset!', 'error')
            except Exception as e:
                flash(f'❌ Error: {str(e)}', 'error')
        
        elif action == 'edit_asset':
            try:
                asset_id = int(request.form.get('asset_id'))
                asset_name = request.form.get('asset_name')
                asset_code = request.form.get('asset_code')
                cost = parse_rupiah(request.form.get('cost'))
                salvage_value = parse_rupiah(request.form.get('salvage_value', '0'))
                useful_life = int(request.form.get('useful_life'))
                method = request.form.get('method')
                purchase_date = request.form.get('purchase_date')
                
                if update_asset(asset_id, asset_name, asset_code, cost, salvage_value, useful_life, method, purchase_date):
                    flash('✅ Aset berhasil diupdate!', 'success')
                else:
                    flash('❌ Gagal mengupdate aset!', 'error')
            except Exception as e:
                flash(f'❌ Error: {str(e)}', 'error')
        
        elif action == 'delete_asset':
            try:
                asset_id = int(request.form.get('asset_id'))
                if delete_asset(asset_id):
                    flash('✅ Aset berhasil dihapus!', 'success')
                else:
                    flash('❌ Gagal menghapus aset!', 'error')
            except Exception as e:
                flash(f'❌ Error: {str(e)}', 'error')
        
        elif action == 'calculate_depreciation':
            try:
                asset_id = int(request.form.get('asset_id'))
                period_year = int(request.form.get('period_year', 1))
                period_type = request.form.get('period_type', 'annual')
                
                asset = get_asset_by_id(asset_id)
                if asset:
                    depreciation = calculate_depreciation(asset, period_year, period_type)
                    period_label = 'bulan' if period_type == 'monthly' else 'tahun'
                    flash(f'✅ Penyusutan {period_label} ke-{period_year}: {format_rupiah(depreciation)}', 'success')
                else:
                    flash('❌ Aset tidak ditemukan!', 'error')
            except Exception as e:
                flash(f'❌ Error: {str(e)}', 'error')
        
        elif action == 'record_depreciation':
            try:
                asset_id = int(request.form.get('asset_id'))
                period_year = int(request.form.get('period_year', 1))
                period_type = request.form.get('period_type', 'annual')
                period_date_str = request.form.get('period_date')
                period_date = datetime.strptime(period_date_str, '%Y-%m-%d')
                
                asset = get_asset_by_id(asset_id)
                if asset:
                    depreciation = calculate_depreciation(asset, period_year, period_type)
                    if record_depreciation_entry(asset, depreciation, period_date):
                        flash(f'✅ Jurnal penyusutan berhasil dicatat ke Jurnal Penyesuaian: {format_rupiah(depreciation)}', 'success')
                    else:
                        flash('❌ Gagal mencatat jurnal penyusutan!', 'error')
                else:
                    flash('❌ Aset tidak ditemukan!', 'error')
            except Exception as e:
                flash(f'❌ Error: {str(e)}', 'error')
        
        return redirect(url_for('akuntan_assets'))
    
    # ... (sisa kode GET method tetap sama)
    
    username = session.get('username', 'User')
    assets = get_all_assets()
    
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    # Generate assets table
    assets_html = ""
    for asset in assets:
        purchase_date = datetime.fromisoformat(asset['purchase_date']) if asset.get('purchase_date') else datetime.now()
        years_used = (datetime.now() - purchase_date).days // 365
        
        method_translate = {
            'straight_line': 'Garis Lurus',
            'declining_balance': 'Saldo Menurun',
            'sum_of_years': 'Jumlah Angka Tahun'
        }
        method_name = method_translate.get(asset['depreciation_method'], asset['depreciation_method'])
        
        # Convert asset data to JSON for edit modal
        asset_json = {
            'id': asset['id'],
            'asset_code': asset['asset_code'],
            'asset_name': asset['asset_name'],
            'cost': asset['cost'],
            'salvage_value': asset.get('salvage_value', 0),
            'useful_life': asset['useful_life'],
            'method': asset['depreciation_method'],
            'purchase_date': asset['purchase_date']
        }
        import json
        asset_data = json.dumps(asset_json).replace('"', '&quot;')
        
        assets_html += f"""
        <tr>
            <td class="text-center"><strong>{asset['asset_code']}</strong></td>
            <td>{asset['asset_name']}</td>
            <td class="text-right">{format_rupiah(asset['cost'])}</td>
            <td class="text-center">{asset['useful_life']} tahun</td>
            <td class="text-center">{method_name}</td>
            <td class="text-right">{format_rupiah(asset.get('accumulated_depreciation', 0))}</td>
            <td class="text-right"><strong>{format_rupiah(asset.get('book_value', asset['cost']))}</strong></td>
            <td class="text-center">
                <div class="btn-group">
                    <button class="btn-sm btn-info" onclick="showDepreciationModal({asset['id']}, '{asset['asset_name']}', {years_used + 1})" title="Hitung Penyusutan">
                        📊
                    </button>
                    <button class="btn-sm btn-success" onclick="showRecordModal({asset['id']}, '{asset['asset_name']}', {years_used + 1})" title="Catat Jurnal">
                        💾
                    </button>
                    <button class="btn-sm btn-warning" onclick='showEditModal({asset_data})' title="Edit Aset">
                        ✏️
                    </button>
                    <button class="btn-sm btn-danger" onclick="confirmDelete({asset['id']}, '{asset['asset_name']}')" title="Hapus Aset">
                        🗑️
                    </button>
                </div>
            </td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Aset & Penyusutan - Geboy Mujair</title>
        {generate_dashboard_style()}
        <style>
            .btn-group {{
                display: flex;
                gap: 5px;
                justify-content: center;
                flex-wrap: wrap;
            }}
            .btn-group .btn-sm {{
                padding: 6px 10px;
                font-size: 14px;
            }}
        </style>
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">📊</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Akuntan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/akuntan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/akuntan/accounts"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/manual-transaction"><span class="icon">➕</span> Transaksi Manual</a></li>
                    <li><a href="/akuntan/inventory-card"><span class="icon">📦</span> Inventory Card</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Penutupan</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Pembalikan</a></li>
                    <li><a href="/akuntan/assets"><span class="icon">🏢</span> Aset</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> NS</a></li>
                    <li><a href="/akuntan/adjusted-trial-balance"><span class="icon">✅</span> NS Penyesuaian</a></li>
                    <li><a href="/akuntan/worksheet"><span class="icon">📊</span> Neraca Lajur</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">💼</span> Lap. Keuangan</a></li>
                    <li><a href="/akuntan/cash-flow-statement"><span class="icon">💰</span> Arus Kas</a></li>
                    <li><a href="/akuntan/post-closing-trial-balance"><span class="icon">🔐</span> NS Penutupan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Aset Tetap & Penyusutan</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <!-- ============= FORM TAMBAH ASET ============= -->
                <div class="content-section">
                    <h2>➕ Tambah Aset Baru</h2>
                    {flash_html}
                    <form method="POST">
                        <input type="hidden" name="action" value="add_asset">
                        
                        <div class="form-row">
                            <div class="form-group">
                                <label>Kode Aset *</label>
                                <input type="text" name="asset_code" required placeholder="AST-001">
                            </div>
                            <div class="form-group">
                                <label>Nama Aset *</label>
                                <input type="text" name="asset_name" required placeholder="Kolam Ikan Besar">
                            </div>
                        </div>
                        
                        <div class="form-row">
                            <div class="form-group">
                                <label>Harga Perolehan *</label>
                                <input type="text" name="cost" required placeholder="Rp0,00" class="rupiah-input">
                            </div>
                            <div class="form-group">
                                <label>Nilai Residu</label>
                                <input type="text" name="salvage_value" placeholder="Rp0,00" class="rupiah-input">
                                <small style="color: #666;">Nilai sisa aset di akhir umur ekonomis</small>
                            </div>
                        </div>
                        
                        <div class="form-row">
                            <div class="form-group">
                                <label>Tanggal Pembelian *</label>
                                <input type="date" name="purchase_date" required value="{datetime.now().strftime('%Y-%m-%d')}">
                            </div>
                            <div class="form-group">
                                <label>Umur Ekonomis (Tahun) *</label>
                                <input type="number" name="useful_life" required min="1" placeholder="5">
                                <small style="color: #666;">Estimasi masa pakai aset</small>
                            </div>
                        </div>
                        
                        <div class="form-group">
                            <label>Metode Penyusutan *</label>
                            <select name="method" required>
                                <option value="">-- Pilih Metode --</option>
                                <option value="straight_line">Garis Lurus (Straight Line)</option>
                                <option value="declining_balance">Saldo Menurun (Declining Balance)</option>
                                <option value="sum_of_years">Jumlah Angka Tahun (Sum of Years Digits)</option>
                            </select>
                        </div>
                        
                        <button type="submit" class="btn-sm btn-success btn-block">💾 Tambah Aset</button>
                    </form>
                </div>
                
                <!-- ============= TABEL DAFTAR ASET ============= -->
                <div class="content-section">
                    <h2>🏢 Daftar Aset Tetap</h2>
                    <table>
                        <thead>
                            <tr>
                                <th class="text-center">Kode</th>
                                <th>Nama Aset</th>
                                <th class="text-right">Harga Perolehan</th>
                                <th class="text-center">Umur Ekonomis</th>
                                <th class="text-center">Metode</th>
                                <th class="text-right">Akum. Penyusutan</th>
                                <th class="text-right">Nilai Buku</th>
                                <th class="text-center">Aksi</th>
                            </tr>
                        </thead>
                        <tbody>
                            {assets_html if assets_html else '<tr><td colspan="8" class="text-center">Belum ada aset</td></tr>'}
                        </tbody>
                    </table>
                </div>
                
                <!-- ============= PENJELASAN METODE ============= -->
                <div class="content-section" style="background: #f8f9fa; border-left: 4px solid #667eea;">
                    <h3 style="color: #667eea; margin-bottom: 15px;">📘 Penjelasan Metode Penyusutan</h3>
                    
                    <div style="margin-bottom: 20px;">
                        <h4 style="color: #333; margin-bottom: 10px;">1. Garis Lurus (Straight Line)</h4>
                        <p style="line-height: 1.8; margin-bottom: 5px;">
                            <strong>Formula:</strong> (Harga Perolehan - Nilai Residu) / Umur Ekonomis<br>
                            <strong>Karakteristik:</strong> Penyusutan sama setiap periode<br>
                            <strong>Contoh Tahunan:</strong> Aset Rp10.000.000, Residu Rp1.000.000, Umur 5 tahun<br>
                            → Per tahun = (10.000.000 - 1.000.000) / 5 = <strong>Rp1.800.000</strong><br>
                            <strong>Contoh Bulanan:</strong> Rp1.800.000 / 12 = <strong>Rp150.000/bulan</strong>
                        </p>
                    </div>
                    
                    <div style="margin-bottom: 20px;">
                        <h4 style="color: #333; margin-bottom: 10px;">2. Saldo Menurun (Declining Balance)</h4>
                        <p style="line-height: 1.8; margin-bottom: 5px;">
                            <strong>Formula:</strong> Nilai Buku × (2 / Umur Ekonomis)<br>
                            <strong>Karakteristik:</strong> Penyusutan lebih besar di tahun awal<br>
                            <strong>Contoh Tahunan:</strong> Aset Rp10.000.000, Umur 5 tahun<br>
                            → Tahun 1: 10.000.000 × (2/5) = <strong>Rp4.000.000</strong><br>
                            → Tahun 2: 6.000.000 × (2/5) = <strong>Rp2.400.000</strong>
                        </p>
                    </div>
                    
                    <div>
                        <h4 style="color: #333; margin-bottom: 10px;">3. Jumlah Angka Tahun (Sum of Years Digits)</h4>
                        <p style="line-height: 1.8;">
                            <strong>Formula:</strong> (Sisa Umur / Jumlah Angka Tahun) × (Cost - Salvage)<br>
                            <strong>Karakteristik:</strong> Penyusutan menurun secara bertahap<br>
                            <strong>Contoh Tahunan:</strong> Aset Rp10.000.000, Residu Rp1.000.000, Umur 5 tahun<br>
                            → Jumlah angka tahun = 5+4+3+2+1 = 15<br>
                            → Tahun 1: (5/15) × 9.000.000 = <strong>Rp3.000.000</strong><br>
                            → Tahun 2: (4/15) × 9.000.000 = <strong>Rp2.400.000</strong>
                        </p>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- ============= MODAL EDIT ASET ============= -->
        <div id="editModal" class="modal">
            <div class="modal-content">
                <span class="close" onclick="closeModal('editModal')">&times;</span>
                <h2>✏️ Edit Aset</h2>
                <form method="POST" id="editForm">
                    <input type="hidden" name="action" value="edit_asset">
                    <input type="hidden" name="asset_id" id="edit_asset_id">
                    
                    <div class="form-row">
                        <div class="form-group">
                            <label>Kode Aset *</label>
                            <input type="text" name="asset_code" id="edit_asset_code" required>
                        </div>
                        <div class="form-group">
                            <label>Nama Aset *</label>
                            <input type="text" name="asset_name" id="edit_asset_name" required>
                        </div>
                    </div>
                    
                    <div class="form-row">
                        <div class="form-group">
                            <label>Harga Perolehan *</label>
                            <input type="text" name="cost" id="edit_cost" required class="rupiah-input">
                        </div>
                        <div class="form-group">
                            <label>Nilai Residu</label>
                            <input type="text" name="salvage_value" id="edit_salvage_value" class="rupiah-input">
                        </div>
                    </div>
                    
                    <div class="form-row">
                        <div class="form-group">
                            <label>Tanggal Pembelian *</label>
                            <input type="date" name="purchase_date" id="edit_purchase_date" required>
                        </div>
                        <div class="form-group">
                            <label>Umur Ekonomis (Tahun) *</label>
                            <input type="number" name="useful_life" id="edit_useful_life" required min="1">
                        </div>
                    </div>
                    
                    <div class="form-group">
                        <label>Metode Penyusutan *</label>
                        <select name="method" id="edit_method" required>
                            <option value="straight_line">Garis Lurus (Straight Line)</option>
                            <option value="declining_balance">Saldo Menurun (Declining Balance)</option>
                            <option value="sum_of_years">Jumlah Angka Tahun (Sum of Years Digits)</option>
                        </select>
                    </div>
                    
                    <button type="submit" class="btn-sm btn-warning btn-block">💾 Update Aset</button>
                </form>
            </div>
        </div>
        
        <!-- ============= MODAL HAPUS ASET ============= -->
        <div id="deleteModal" class="modal">
            <div class="modal-content" style="max-width: 400px;">
                <span class="close" onclick="closeModal('deleteModal')">&times;</span>
                <h2 style="color: #e74c3c;">🗑️ Hapus Aset</h2>
                <form method="POST" id="deleteForm">
                    <input type="hidden" name="action" value="delete_asset">
                    <input type="hidden" name="asset_id" id="delete_asset_id">
                    
                    <p style="margin: 20px 0; text-align: center; font-size: 16px;">
                        Yakin ingin menghapus aset:<br>
                        <strong id="delete_asset_name" style="color: #667eea;"></strong>?
                    </p>
                    
                    <div style="display: flex; gap: 10px;">
                        <button type="button" class="btn-sm" onclick="closeModal('deleteModal')" style="flex: 1; background: #95a5a6;">
                            ❌ Batal
                        </button>
                        <button type="submit" class="btn-sm btn-danger" style="flex: 1;">
                            🗑️ Hapus
                        </button>
                    </div>
                </form>
            </div>
        </div>
        
        <!-- ============= MODAL HITUNG PENYUSUTAN ============= -->
        <div id="depreciationModal" class="modal">
            <div class="modal-content">
                <span class="close" onclick="closeModal('depreciationModal')">&times;</span>
                <h2>📊 Hitung Penyusutan</h2>
                <form method="POST" id="calculateForm">
                    <input type="hidden" name="action" value="calculate_depreciation">
                    <input type="hidden" name="asset_id" id="calc_asset_id">
                    
                    <div class="form-group">
                        <label>Aset</label>
                        <input type="text" id="calc_asset_name" readonly style="background: #f0f0f0;">
                    </div>
                    
                    <div class="form-group">
                        <label>Periode Penyusutan *</label>
                        <select name="period_type" id="calc_period_type" required onchange="updatePeriodLabel('calc')">
                            <option value="annual">Per Tahun</option>
                            <option value="monthly">Per Bulan</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label id="calc_period_label">Tahun Ke-</label>
                        <input type="number" name="period_year" id="calc_period_year" min="1" required>
                        <small style="color: #666;" id="calc_period_hint">Periode ke-berapa yang ingin dihitung</small>
                    </div>
                    
                    <button type="submit" class="btn-sm btn-primary btn-block">🔢 Hitung Penyusutan</button>
                </form>
            </div>
        </div>
        
        <!-- ============= MODAL CATAT PENYUSUTAN ============= -->
        <div id="recordModal" class="modal">
            <div class="modal-content">
                <span class="close" onclick="closeModal('recordModal')">&times;</span>
                <h2>💾 Catat Jurnal Penyusutan</h2>
                <form method="POST" id="recordForm">
                    <input type="hidden" name="action" value="record_depreciation">
                    <input type="hidden" name="asset_id" id="record_asset_id">
                    
                    <div class="form-group">
                        <label>Aset</label>
                        <input type="text" id="record_asset_name" readonly style="background: #f0f0f0;">
                    </div>
                    
                    <div class="form-group">
                        <label>Periode Penyusutan *</label>
                        <select name="period_type" id="record_period_type" required onchange="updatePeriodLabel('record')">
                            <option value="annual">Per Tahun</option>
                            <option value="monthly">Per Bulan</option>
                        </select>
                    </div>
                    
                    <div class="form-group">
                        <label id="record_period_label">Tahun Ke-</label>
                        <input type="number" name="period_year" id="record_period_year" min="1" required>
                        <small style="color: #666;" id="record_period_hint">Periode ke-berapa yang ingin dicatat</small>
                    </div>
                    
                    <div class="form-group">
                        <label>Tanggal Pencatatan *</label>
                        <input type="date" name="period_date" required value="{datetime.now().strftime('%Y-%m-%d')}">
                    </div>
                    
                    <button type="submit" class="btn-sm btn-success btn-block">💾 Catat Jurnal</button>
                </form>
            </div>
        </div>
        
        <script>
        // Format rupiah input
        document.querySelectorAll('.rupiah-input').forEach(input => {{
            input.addEventListener('blur', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = 'Rp' + parseInt(val).toLocaleString('id-ID') + ',00';
                }}
            }});
            
            input.addEventListener('focus', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = val;
                }}
            }});
        }});
        
        // Update period label based on selection
        function updatePeriodLabel(prefix) {{
            const periodType = document.getElementById(prefix + '_period_type').value;
            const label = document.getElementById(prefix + '_period_label');
            const hint = document.getElementById(prefix + '_period_hint');
            
            if (periodType === 'monthly') {{
                label.textContent = 'Bulan Ke-';
                hint.textContent = 'Bulan ke-berapa yang ingin ' + (prefix === 'calc' ? 'dihitung' : 'dicatat');
            }} else {{
                label.textContent = 'Tahun Ke-';
                hint.textContent = 'Tahun ke-berapa yang ingin ' + (prefix === 'calc' ? 'dihitung' : 'dicatat');
            }}
        }}
        
        // Show edit modal
        function showEditModal(assetData) {{
            document.getElementById('edit_asset_id').value = assetData.id;
            document.getElementById('edit_asset_code').value = assetData.asset_code;
            document.getElementById('edit_asset_name').value = assetData.asset_name;
            document.getElementById('edit_cost').value = 'Rp' + parseInt(assetData.cost).toLocaleString('id-ID') + ',00';
            document.getElementById('edit_salvage_value').value = 'Rp' + parseInt(assetData.salvage_value).toLocaleString('id-ID') + ',00';
            document.getElementById('edit_useful_life').value = assetData.useful_life;
            document.getElementById('edit_method').value = assetData.method;
            document.getElementById('edit_purchase_date').value = assetData.purchase_date;
            document.getElementById('editModal').style.display = 'block';
        }}
        
        // Confirm delete
        function confirmDelete(assetId, assetName) {{
            document.getElementById('delete_asset_id').value = assetId;
            document.getElementById('delete_asset_name').textContent = assetName;
            document.getElementById('deleteModal').style.display = 'block';
        }}
        
        // Show depreciation calculation modal
        function showDepreciationModal(assetId, assetName, periodYear) {{
            document.getElementById('calc_asset_id').value = assetId;
            document.getElementById('calc_asset_name').value = assetName;
            document.getElementById('calc_period_year').value = periodYear;
            document.getElementById('depreciationModal').style.display = 'block';
        }}
        
        // Show record depreciation modal
        function showRecordModal(assetId, assetName, periodYear) {{
            document.getElementById('record_asset_id').value = assetId;
            document.getElementById('record_asset_name').value = assetName;
            document.getElementById('record_period_year').value = periodYear;
            document.getElementById('recordModal').style.display = 'block';
        }}
        
        // Close modal
        function closeModal(modalId) {{
            document.getElementById(modalId).style.display = 'none';
        }}
        
        // Close modal when clicking outside
        window.onclick = function(event) {{
            if (event.target.className === 'modal') {{
                event.target.style.display = 'none';
            }}
        }}
        
        // Update datetime display
        function updateDateTime() {{
            const now = new Date();
            const options = {{ 
                weekday: 'long', 
                year: 'numeric', 
                month: 'long', 
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            }};
            document.getElementById('datetime').textContent = now.toLocaleDateString('id-ID', options);
        }}
        updateDateTime();
        setInterval(updateDateTime, 60000);
        </script>
    </body>
    </html>
    """
    
    return html


# ============= HELPER FUNCTIONS =============
def update_asset(asset_id, asset_name, asset_code, cost, salvage_value, useful_life, method, purchase_date):
    """Update aset yang sudah ada"""
    try:
        data = {
            'asset_name': asset_name,
            'asset_code': asset_code,
            'cost': float(cost),
            'salvage_value': float(salvage_value),
            'useful_life': int(useful_life),
            'depreciation_method': method,
            'purchase_date': purchase_date,
            'book_value': float(cost),  # Reset book value saat edit
            'updated_at': datetime.now().isoformat()
        }
        response = supabase.table('assets').update(data).eq('id', asset_id).execute()
        return True if response.data else False
    except Exception as e:
        print(f"❌ Error update_asset: {e}")
        import traceback
        traceback.print_exc()
        return False
def delete_asset(asset_id):
    """Hapus aset dan semua jurnal terkait"""
    try:
        # Ambil ref_code pattern untuk aset ini
        ref_pattern = f'DEP{asset_id}-%'
        
        # Hapus semua jurnal penyusutan terkait
        supabase.table('journal_entries').delete().like('ref_code', ref_pattern).execute()
        
        # Hapus asset
        response = supabase.table('assets').delete().eq('id', asset_id).execute()
        return True
    except Exception as e:
        print(f"❌ Error delete_asset: {e}")
        import traceback
        traceback.print_exc()
        return False

def calculate_depreciation(asset, period, period_type='annual'):
    """
    Calculate depreciation based on method and period type
    
    Args:
        asset: Asset dictionary
        period: Period number (year or month depending on period_type)
        period_type: 'annual' or 'monthly'
    
    Returns:
        Depreciation amount for the specified period
    """
    cost = float(asset['cost'])
    salvage = float(asset.get('salvage_value', 0))
    useful_life = int(asset['useful_life'])
    method = asset['depreciation_method']
    
    if method == 'straight_line':
        # Straight Line Method
        annual_depreciation = (cost - salvage) / useful_life
        
        if period_type == 'monthly':
            # Monthly depreciation
            monthly_depreciation = annual_depreciation / 12
            return monthly_depreciation
        else:
            # Annual depreciation
            return annual_depreciation
    
    elif method == 'declining_balance':
        # Declining Balance Method (Double Declining)
        rate = 2 / useful_life
        book_value = cost
        
        if period_type == 'monthly':
            # Calculate monthly depreciation
            monthly_rate = rate / 12
            for i in range(1, period + 1):
                depreciation = book_value * monthly_rate
                # Don't depreciate below salvage value
                if book_value - depreciation < salvage:
                    depreciation = max(0, book_value - salvage)
                book_value -= depreciation
                if i == period:
                    return max(0, depreciation)
        else:
            # Calculate annual depreciation
            for i in range(1, period + 1):
                depreciation = book_value * rate
                # Don't depreciate below salvage value
                if book_value - depreciation < salvage:
                    depreciation = max(0, book_value - salvage)
                book_value -= depreciation
                if i == period:
                    return max(0, depreciation)
    
    elif method == 'sum_of_years':
        # Sum of Years Digits Method
        sum_of_years = (useful_life * (useful_life + 1)) / 2
        depreciable_amount = cost - salvage
        
        if period_type == 'monthly':
            # For monthly calculation, determine which year and month
            year = (period - 1) // 12 + 1
            
            if year <= useful_life:
                remaining_life = useful_life - year + 1
                annual_depreciation = (remaining_life / sum_of_years) * depreciable_amount
                monthly_depreciation = annual_depreciation / 12
                return monthly_depreciation
            else:
                return 0
        else:
            # Annual calculation
            if period <= useful_life:
                remaining_life = useful_life - period + 1
                return (remaining_life / sum_of_years) * depreciable_amount
            else:
                return 0
    
    return 0
    
#==============Dashboard===============
def generate_akuntan_dashboard():
    """Generate dashboard akuntan dengan menu lengkap"""
    username = session.get('username', 'User')
    
    accounts = get_all_accounts()
    
    # ✅ HITUNG DARI NERACA SALDO (bukan dari jurnal langsung)
    trial_balance = get_trial_balance()
    total_debit = sum(float(tb['debit']) for tb in trial_balance)
    total_credit = sum(float(tb['credit']) for tb in trial_balance)
    is_balanced = abs(total_debit - total_credit) < 0.01
    
    # Hitung total jurnal entries untuk statistik
    journals = get_journal_entries()
    total_journal_entries = len(journals)
    
    # ✅ HITUNG TOTAL AKUN DEBIT DAN KREDIT
    total_debit_accounts = len([acc for acc in accounts if acc['normal_balance'] == 'debit'])
    total_credit_accounts = len([acc for acc in accounts if acc['normal_balance'] == 'credit'])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dashboard Akuntan - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            {generate_sidebar('akuntan', username, 'dashboard')}
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Dashboard Akuntan</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <!-- STATS ROW 1: AKUN & JURNAL -->
                <div class="stats-grid" style="grid-template-columns: repeat(4, 1fr);">
                    <div class="stat-card">
                        <div class="stat-icon">📋</div>
                        <div class="stat-value">{len(accounts)}</div>
                        <div class="stat-label">Total Akun</div>
                    </div>
                    <div class="stat-card" style="background: linear-gradient(135deg, #28a745 0%, #20c997 100%);">
                        <div class="stat-icon">📗</div>
                        <div class="stat-value">{total_debit_accounts}</div>
                        <div class="stat-label">Akun Debit</div>
                    </div>
                    <div class="stat-card" style="background: linear-gradient(135deg, #dc3545 0%, #e83e8c 100%);">
                        <div class="stat-icon">📕</div>
                        <div class="stat-value">{total_credit_accounts}</div>
                        <div class="stat-label">Akun Kredit</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📝</div>
                        <div class="stat-value">{total_journal_entries}</div>
                        <div class="stat-label">Total Jurnal Entry</div>
                    </div>
                </div>
                
                <!-- STATS ROW 2: NERACA SALDO -->
                <div class="stats-grid" style="grid-template-columns: repeat(2, 1fr); margin-top: 20px;">
                    <div class="stat-card" style="background: linear-gradient(135deg, #28a745 0%, #218838 100%);">
                        <div class="stat-icon">💵</div>
                        <div class="stat-value">{format_rupiah(total_debit)}</div>
                        <div class="stat-label">Total Debit (Neraca Saldo)</div>
                    </div>
                    <div class="stat-card" style="background: linear-gradient(135deg, #dc3545 0%, #c82333 100%);">
                        <div class="stat-icon">💸</div>
                        <div class="stat-value">{format_rupiah(total_credit)}</div>
                        <div class="stat-label">Total Kredit (Neraca Saldo)</div>
                    </div>
                </div>
                
                <!-- STATUS BALANCE -->
                <div class="content-section" style="background: {'#d4edda' if is_balanced else '#fff3cd'}; border-left: 4px solid {'#28a745' if is_balanced else '#ffc107'};">
                    <h3 style="color: {'#155724' if is_balanced else '#856404'}; margin-bottom: 10px;">
                        {'✅ Neraca Saldo Balance' if is_balanced else '⚠️ Neraca Saldo Belum Balance'}
                    </h3>
                    <p style="color: {'#155724' if is_balanced else '#856404'}; margin: 0;">
                        {f'Total Debit = Total Kredit ({format_rupiah(total_debit)})' if is_balanced else f'Selisih: {format_rupiah(abs(total_debit - total_credit))}'}
                    </p>
                </div>
                
                <!-- BREAKDOWN AKUN BY TYPE -->
                <div class="content-section">
                    <h2>📊 Breakdown Chart of Accounts</h2>
                    <div style="display: grid; grid-template-columns: repeat(5, 1fr); gap: 15px;">
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; text-align: center; border-left: 4px solid #667eea;">
                            <h3 style="color: #667eea; font-size: 32px; margin-bottom: 5px;">
                                {len([a for a in accounts if a['account_code'].startswith('1-')])}
                            </h3>
                            <p style="color: #666; margin: 0; font-size: 14px;">Aset (1-xxxx)</p>
                        </div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; text-align: center; border-left: 4px solid #ffc107;">
                            <h3 style="color: #ffc107; font-size: 32px; margin-bottom: 5px;">
                                {len([a for a in accounts if a['account_code'].startswith('2-')])}
                            </h3>
                            <p style="color: #666; margin: 0; font-size: 14px;">Kewajiban (2-xxxx)</p>
                        </div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; text-align: center; border-left: 4px solid #17a2b8;">
                            <h3 style="color: #17a2b8; font-size: 32px; margin-bottom: 5px;">
                                {len([a for a in accounts if a['account_code'].startswith('3-')])}
                            </h3>
                            <p style="color: #666; margin: 0; font-size: 14px;">Ekuitas (3-xxxx)</p>
                        </div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; text-align: center; border-left: 4px solid #28a745;">
                            <h3 style="color: #28a745; font-size: 32px; margin-bottom: 5px;">
                                {len([a for a in accounts if a['account_code'].startswith('4-')])}
                            </h3>
                            <p style="color: #666; margin: 0; font-size: 14px;">Pendapatan (4-xxxx)</p>
                        </div>
                        <div style="background: #f8f9fa; padding: 20px; border-radius: 10px; text-align: center; border-left: 4px solid #dc3545;">
                            <h3 style="color: #dc3545; font-size: 32px; margin-bottom: 5px;">
                                {len([a for a in accounts if a['account_code'].startswith('5-') or a['account_code'].startswith('6-')])}
                            </h3>
                            <p style="color: #666; margin: 0; font-size: 14px;">Beban (5/6-xxxx)</p>
                        </div>
                    </div>
                </div>
                
                <!-- SIKLUS AKUNTANSI -->
                <div class="content-section">
                    <h2>📊 Siklus Akuntansi</h2>
                    <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px;">
                        <a href="/akuntan/accounts" class="btn-sm btn-primary btn-block">1️⃣ Daftar Akun</a>
                        <a href="/akuntan/journal-gj" class="btn-sm btn-success btn-block">2️⃣ Jurnal Umum</a>
                        <a href="/akuntan/ledger" class="btn-sm btn-info btn-block">3️⃣ Buku Besar</a>
                        <a href="/akuntan/trial-balance" class="btn-sm btn-warning btn-block">4️⃣ Neraca Saldo</a>
                        <a href="/akuntan/adjustment-journal" class="btn-sm btn-primary btn-block">5️⃣ Penyesuaian</a>
                        <a href="/akuntan/worksheet" class="btn-sm btn-info btn-block">6️⃣ Neraca Lajur</a>
                        <a href="/akuntan/financial-statements" class="btn-sm btn-success btn-block">7️⃣ Laporan Keuangan</a>
                        <a href="/akuntan/closing-journal" class="btn-sm btn-danger btn-block">8️⃣ Penutupan</a>
                    </div>
                </div>
                
                <!-- FITUR TAMBAHAN -->
                <div class="content-section">
                    <h2>🔧 Fitur Tambahan</h2>
                    <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px;">
                        <a href="/akuntan/manual-transaction" class="btn-sm btn-info btn-block">➕ Transaksi Manual</a>
                        <a href="/akuntan/inventory-card" class="btn-sm btn-warning btn-block">📦 Inventory Card</a>
                        <a href="/akuntan/assets" class="btn-sm btn-primary btn-block">🏢 Aset & Penyusutan</a>
                        <a href="/akuntan/reversing-journal" class="btn-sm btn-secondary btn-block">🔄 Pembalikan</a>
                    </div>
                </div>
                
                <!-- QUICK INFO -->
                <div class="content-section">
                    <h2>📌 Informasi Sistem</h2>
                    <div style="background: #f8f9fa; padding: 20px; border-radius: 10px;">
                        <ul style="line-height: 2; margin-left: 20px;">
                            <li><strong>Metode Pencatatan:</strong> Perpetual (otomatis update persediaan)</li>
                            <li><strong>Transaksi Kasir:</strong> Otomatis masuk Jurnal Umum + HPP</li>
                            <li><strong>Pembelian Karyawan:</strong> Otomatis masuk Jurnal Umum + Inventory</li>
                            <li><strong>Posting Buku Besar:</strong> Otomatis dari Jurnal Umum</li>
                            <li><strong>Total Akun Aktif:</strong> {len(accounts)} akun ({total_debit_accounts} Debit, {total_credit_accounts} Kredit)</li>
                        </ul>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html

def generate_karyawan_dashboard():
    """Generate dashboard karyawan"""
    username = session.get('username', 'User')
    
    # Ambil pembelian karyawan ini
    purchases = [p for p in get_purchases() if p.get('employee_username') == username]
    total_purchases = sum(float(p['total_amount']) for p in purchases)
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dashboard Karyawan - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">👷</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Karyawan</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/karyawan" class="active"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/karyawan/purchase"><span class="icon">🛒</span> Pembelian</a></li>
                    <li><a href="/karyawan/purchase-history"><span class="icon">📋</span> Riwayat Pembelian</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Dashboard Karyawan</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">🛒</div>
                        <div class="stat-value">{len(purchases)}</div>
                        <div class="stat-label">Total Pembelian</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">💰</div>
                        <div class="stat-value">{format_rupiah(total_purchases)}</div>
                        <div class="stat-label">Total Pengeluaran</div>
                    </div>
                </div>
                
                <div class="content-section">
                    <h2>🛒 Pembelian Terbaru</h2>
                    <a href="/karyawan/purchase" class="btn-sm btn-success" style="margin-bottom: 20px;">➕ Pembelian Baru</a>
                    <table>
                        <thead>
                            <tr>
                                <th>Tanggal</th>
                                <th>Jenis</th>
                                <th>Item</th>
                                <th class="text-center">Jumlah</th>
                                <th class="text-right">Total</th>
                                <th class="text-center">Status</th>
                            </tr>
                        </thead>
                        <tbody>
                            {''.join([f'''
                            <tr>
                                <td>{datetime.fromisoformat(p["date"].replace("Z", "+00:00")).strftime("%d/%m/%Y %H:%M")}</td>
                                <td style="text-transform: capitalize;">{p["item_type"]}</td>
                                <td>{p["item_name"]}</td>
                                <td class="text-center">{p["quantity"]}</td>
                                <td class="text-right">{format_rupiah(p["total_amount"])}</td>
                                <td class="text-center"><span style="background: #28a745; color: white; padding: 5px 10px; border-radius: 5px; font-size: 12px;">✓ Approved</span></td>
                            </tr>
                            ''' for p in purchases[:10]]) if purchases else '<tr><td colspan="6" class="text-center">Belum ada pembelian</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html

def generate_owner_dashboard():
    """Generate dashboard owner"""
    username = session.get('username', 'User')
    
    # Ambil data untuk owner
    transactions = get_transactions()
    total_revenue = sum(float(t['total_amount']) for t in transactions)
    
    journals = get_journal_entries()
    total_expenses = sum(float(j.get('debit', 0)) for j in journals if j['account_code'].startswith('5-') or j['account_code'].startswith('6-'))
    
    net_income = total_revenue - total_expenses
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Dashboard Owner - Geboy Mujair</title>
        {generate_dashboard_style()}
    </head>
    <body>
        <div class="dashboard-container">
            <div class="sidebar">
                <div class="sidebar-header">
                    <div class="sidebar-logo">🐟</div>
                    <div class="sidebar-title">Geboy Mujair</div>
                    <div class="sidebar-subtitle">Sistem Akuntansi</div>
                </div>
                
                <div class="sidebar-user">
                    <div class="sidebar-user-icon">👔</div>
                    <div class="sidebar-user-name">{username}</div>
                    <div class="sidebar-user-role">Owner</div>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/owner" class="active"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/owner/analytics"><span class="icon">📈</span> Analytics</a></li>
                    <li><a href="/owner/financial-reports"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/owner/users"><span class="icon">👥</span> Manajemen User</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Dashboard Owner</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">💵</div>
                        <div class="stat-value">{format_rupiah(total_revenue)}</div>
                        <div class="stat-label">Total Pendapatan</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">💸</div>
                        <div class="stat-value">{format_rupiah(total_expenses)}</div>
                        <div class="stat-label">Total Pengeluaran</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📈</div>
                        <div class="stat-value">{format_rupiah(net_income)}</div>
                        <div class="stat-label">Laba Bersih</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📝</div>
                        <div class="stat-value">{len(transactions)}</div>
                        <div class="stat-label">Total Transaksi</div>
                    </div>
                </div>
                
                <div class="content-section">
                    <h2>📊 Ringkasan Bisnis</h2>
                    <p>Selamat datang di dashboard owner. Anda dapat melihat seluruh performa bisnis budidaya ikan mujair di sini.</p>
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin-top: 20px;">
                        <a href="/owner/analytics" class="btn-sm btn-primary btn-block">📈 Analytics</a>
                        <a href="/owner/financial-reports" class="btn-sm btn-success btn-block">📊 Laporan Keuangan</a>
                        <a href="/owner/users" class="btn-sm btn-info btn-block">👥 Manajemen User</a>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html
# ============== ROUTES - AUTH ==============

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        role = request.form.get('role')
        
        # Validasi email
        if not email or '@' not in email:
            flash('Email tidak valid!', 'error')
            return redirect(url_for('register'))
        
        # Cek apakah email sudah terdaftar
        if get_user_by_email(email):
            flash('Email sudah terdaftar!', 'error')
            return redirect(url_for('register'))
        
        # Generate token untuk verifikasi email
        token = serializer.dumps(email, salt='email-verification')
        
        # Simpan data sementara di Supabase
        if not create_pending_registration(email, role, token):
            flash('Gagal menyimpan data registrasi!', 'error')
            return redirect(url_for('register'))
        
        # Kirim email verifikasi
        verify_url = url_for('verify_email', token=token, _external=True)
        html = f"""
        <h2>Verifikasi Email Geboy Mujair</h2>
        <p>Terima kasih telah mendaftar!</p>
        <p>Klik link di bawah untuk melanjutkan pendaftaran:</p>
        <p><a href="{verify_url}">Verifikasi Email</a></p>
        <p>Link ini berlaku selama 1 jam.</p>
        """
        
        try:
            send_email(email, 'Verifikasi Email Geboy Mujair', html)
            flash('Email verifikasi telah dikirim! Cek inbox Anda.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            flash(f'Gagal mengirim email: {str(e)}', 'error')
            return redirect(url_for('register'))
    
    role = request.args.get('role', '')
    return generate_register_page(role)

@app.route('/verify/<token>', methods=['GET', 'POST'])
def verify_email(token):
    try:
        # Verifikasi token (expired setelah 1 jam)
        email = serializer.loads(token, salt='email-verification', max_age=3600)
    except SignatureExpired:
        flash('Link verifikasi sudah expired!', 'error')
        return redirect(url_for('register'))
    except BadSignature:
        flash('Link verifikasi tidak valid!', 'error')
        return redirect(url_for('register'))
    
    # Cek apakah pending registration ada
    pending = get_pending_registration(email)
    if not pending:
        flash('Data pendaftaran tidak ditemukan!', 'error')
        return redirect(url_for('register'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        # Validasi username
        if not username or len(username) < 3:
            flash('Username minimal 3 karakter!', 'error')
            return generate_verify_email_page(token)
        
        # Cek username sudah dipakai atau belum
        if get_user_by_username(username):
            flash('Username sudah digunakan!', 'error')
            return generate_verify_email_page(token)
        
        # Validasi password
        if password != confirm_password:
            flash('Password tidak cocok!', 'error')
            return generate_verify_email_page(token)
        
        is_valid, message = validate_password(password)
        if not is_valid:
            flash(message, 'error')
            return generate_verify_email_page(token)
        
        # Buat user baru
        role = pending['role']
        user = create_user(email, username, password, role)
        
        if not user:
            flash('Gagal membuat akun! Coba lagi.', 'error')
            return generate_verify_email_page(token)
        
        # Hapus pending registration
        delete_pending_registration(email)
        
        flash('Registrasi berhasil! Silakan login.', 'success')
        return redirect(url_for('login'))
    
    return generate_verify_email_page(token)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # Cari user berdasarkan username
        user = get_user_by_username(username)
        
        if not user:
            flash('Username atau password salah!', 'error')
            return redirect(url_for('login'))
        
        # Cek password
        if not check_password_hash(user['password_hash'], password):
            flash('Username atau password salah!', 'error')
            return redirect(url_for('login'))
        
        # Login berhasil
        session['logged_in'] = True
        session['username'] = username
        session['role'] = user['role']
        session['email'] = user['email']
        # Buat session bersifat permanent (opsional, 1 hari)
        session.permanent = True
        app.permanent_session_lifetime = timedelta(days=1)

        # Redirect ke dashboard sesuai role
        return redirect(url_for(f'dashboard_{user["role"]}'))
    
    return generate_login_page()

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email')
        
        user = get_user_by_email(email)
        if not user:
            flash('Email tidak terdaftar!', 'error')
            return redirect(url_for('forgot_password'))
        
        # Generate token untuk reset password
        token = serializer.dumps(email, salt='password-reset')
        
        # Kirim email reset password
        reset_url = url_for('reset_password', token=token, _external=True)
        html = f"""
        <h2>Reset Password Geboy Mujair</h2>
        <p>Anda meminta reset password.</p>
        <p>Klik link di bawah untuk membuat password baru:</p>
        <p><a href="{reset_url}">Reset Password</a></p>
        <p>Link ini berlaku selama 1 jam.</p>
        <p>Jika Anda tidak meminta reset password, abaikan email ini.</p>
        """
        
        try:
            send_email(email, 'Reset Password Geboy Mujair', html)
            flash('Link reset password telah dikirim ke email Anda!', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            flash(f'Gagal mengirim email: {str(e)}', 'error')
            return redirect(url_for('forgot_password'))
    
    return generate_forgot_password_page()

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        # Verifikasi token (expired setelah 1 jam)
        email = serializer.loads(token, salt='password-reset', max_age=3600)
    except SignatureExpired:
        flash('Link reset password sudah expired!', 'error')
        return redirect(url_for('forgot_password'))
    except BadSignature:
        flash('Link reset password tidak valid!', 'error')
        return redirect(url_for('forgot_password'))
    
    if request.method == 'POST':
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        # Validasi password
        if password != confirm_password:
            flash('Password tidak cocok!', 'error')
            return generate_reset_password_page(token)
        
        is_valid, message = validate_password(password)
        if not is_valid:
            flash(message, 'error')
            return generate_reset_password_page(token)
        
        # Update password
        if update_user_password(email, password):
            flash('Password berhasil direset! Silakan login.', 'success')
            return redirect(url_for('login'))
        else:
            flash('Gagal reset password! Coba lagi.', 'error')
            return generate_reset_password_page(token)
    
    return generate_reset_password_page(token)

# ============== ROUTES - DASHBOARDS =============
@app.route('/dashboard/kasir')
def dashboard_kasir():
    print("SESSION:", dict(session))  # Sudah benar
    if 'username' not in session or session.get('role') != 'kasir':
        flash('Silakan login terlebih dahulu!', 'error')
        return redirect(url_for('login'))
    return generate_kasir_dashboard()

@app.route('/dashboard/akuntan')
def dashboard_akuntan():
    if 'username' not in session or session.get('role') != 'akuntan':
        flash('Silakan login terlebih dahulu!', 'error')
        return redirect(url_for('login'))
    return generate_akuntan_dashboard()

@app.route('/dashboard/owner')
def dashboard_owner():
    if 'username' not in session or session.get('role') != 'owner':
        flash('Silakan login terlebih dahulu!', 'error')
        return redirect(url_for('login'))
    return generate_owner_dashboard()

@app.route('/dashboard/karyawan')
def dashboard_karyawan():
    if 'username' not in session or session.get('role') != 'karyawan':
        flash('Silakan login terlebih dahulu!', 'error')
        return redirect(url_for('login'))
    return generate_karyawan_dashboard()

@app.route('/akuntan/recap-posting', methods=['POST'])
def akuntan_recap_posting():
    """Posting rekapitulasi jurnal khusus"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        journal_type = request.form.get('journal_type')
        period_month = request.form.get('period_month')  # Format: YYYY-MM
        
        if create_recap_posting(journal_type, period_month):
            return jsonify({'success': True, 'message': f'Rekapitulasi {journal_type} berhasil diposting!'})
        else:
            return jsonify({'success': False, 'message': 'Gagal posting rekapitulasi'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})
    
# ============== ROUTES - KASIR ==============
@app.route('/kasir/pos')
def kasir_pos():
    """Halaman POS Kasir"""
    if 'username' not in session or session.get('role') != 'kasir':
        return redirect(url_for('login'))
    return generate_kasir_pos()

@app.route('/kasir/process', methods=['POST'])
def kasir_process():
    if 'username' not in session or session.get('role') != 'kasir':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    try:
        data = request.get_json()
        items = data.get('items', [])
        
        if not items:
            return jsonify({'success': False, 'message': 'Keranjang kosong'})
        
        total_amount = sum(item['subtotal'] for item in items)
        transaction_code = generate_transaction_code(datetime.now())
        
        transaction = create_transaction(
            transaction_code=transaction_code,
            items=items,
            total_amount=total_amount,
            cashier_username=session['username']
        )
        
        if transaction:
            return jsonify({
                'success': True,
                'transaction_code': transaction_code,
                'message': 'Transaksi berhasil'
            })
        else:
            return jsonify({'success': False, 'message': 'Gagal menyimpan transaksi'})
    
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

# ============== MAIN ==============
@app.route('/logout')
def logout():
    session.clear()
    flash('Anda telah logout!', 'success')
    return redirect(url_for('index'))

if __name__ == '__main__':
    print("="*60)
    print("🐟 GEBOY MUJAIR - Sistem Akuntansi Budidaya Ikan")
    print("="*60)
    print("Server running on: http://0.0.0.0:5000")
    print("="*60)
    app.run(debug=True, host='0.0.0.0', port=5000)