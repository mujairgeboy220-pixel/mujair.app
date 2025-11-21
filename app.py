from flask import Flask, request, redirect, session, flash, url_for, jsonify
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from config import Config
from supabase import create_client, Client
import re
import json
from datetime import datetime, timedelta

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

def get_inventory_card():
    """Ambil kartu persediaan"""
    try:
        response = supabase.table('inventory_card').select('*').order('date').execute()
        return response.data if response.data else []
    except:
        return []

def update_inventory(item_name, quantity, unit_price, transaction_type, ref_code):
    """Update inventory card"""
    try:
        data = {
            'date': datetime.now().isoformat(),
            'item_name': item_name,
            'quantity': float(quantity),
            'unit_price': float(unit_price),
            'transaction_type': transaction_type,
            'ref_code': ref_code
        }
        response = supabase.table('inventory_card').insert(data).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error update_inventory: {e}")
        return None

def calculate_inventory_balance(item_name, method='fifo'):
    """Hitung saldo inventory dengan metode FIFO atau Average"""
    try:
        response = supabase.table('inventory_card').select('*').eq('item_name', item_name).order('date').execute()
        entries = response.data if response.data else []
        
        if method == 'fifo':
            balance = 0
            for entry in entries:
                if entry['transaction_type'] == 'in':
                    balance += float(entry['quantity'])
                else:
                    balance -= float(entry['quantity'])
            return balance
        elif method == 'average':
            total_qty = 0
            total_value = 0
            for entry in entries:
                if entry['transaction_type'] == 'in':
                    total_qty += float(entry['quantity'])
                    total_value += float(entry['quantity']) * float(entry['unit_price'])
            return total_value / total_qty if total_qty > 0 else 0
    except:
        return 0

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
            'journal_type': 'RJ',
            'ref_code': 'REVERSING'
        }
        response = supabase.table('journal_entries').insert(data).execute()
        return response.data[0] if response.data else None
    except:
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

def calculate_depreciation(asset, period_year, period_type='annual'):
    """
    Hitung penyusutan aset
    period_type: 'annual' atau 'monthly'
    """
    cost = float(asset['cost'])
    salvage = float(asset['salvage_value'])
    life = int(asset['useful_life'])
    method = asset['depreciation_method']
    accumulated = float(asset.get('accumulated_depreciation', 0))
    
    depreciable_amount = cost - salvage
    
    if method == 'straight_line':
        annual_depreciation = depreciable_amount / life
        
    elif method == 'declining_balance':
        rate = 2 / life
        book_value = cost - accumulated
        annual_depreciation = book_value * rate
        remaining = cost - salvage - accumulated
        annual_depreciation = min(annual_depreciation, remaining)
        
    elif method == 'sum_of_years':
        sum_of_years = (life * (life + 1)) / 2
        remaining_years = life - period_year + 1
        if remaining_years <= 0:
            return 0
        annual_depreciation = (remaining_years / sum_of_years) * depreciable_amount
    else:
        annual_depreciation = 0
    
    # Jika monthly, bagi 12
    if period_type == 'monthly':
        return annual_depreciation / 12
    
    return annual_depreciation

def record_depreciation_entry(asset, depreciation_amount, period_date):
    """Catat jurnal penyusutan"""
    try:
        ref_code = f"DEP{asset['id']}-{period_date.strftime('%Y%m')}"
        
        # Debit: Beban Penyusutan
        create_journal_entry(
            date=period_date.strftime('%Y-%m-%d'),
            account_code='6-1401',
            account_name='Beban Penyusutan Peralatan',
            description=f"Penyusutan {asset['asset_name']}",
            debit=depreciation_amount,
            credit=0,
            journal_type='AJ',
            ref_code=ref_code
        )
        
        # Credit: Akumulasi Penyusutan
        create_journal_entry(
            date=period_date.strftime('%Y-%m-%d'),
            account_code='1-2102',
            account_name='Akumulasi Penyusutan Peralatan',
            description=f"Penyusutan {asset['asset_name']}",
            debit=0,
            credit=depreciation_amount,
            journal_type='AJ',
            ref_code=ref_code
        )
        
        # Update accumulated depreciation di tabel assets
        new_accumulated = float(asset.get('accumulated_depreciation', 0)) + depreciation_amount
        new_book_value = float(asset['cost']) - new_accumulated
        
        supabase.table('assets').update({
            'accumulated_depreciation': new_accumulated,
            'book_value': new_book_value
        }).eq('id', asset['id']).execute()
        
        return True
    except Exception as e:
        print(f"Error record_depreciation_entry: {e}")
        return False

def get_asset_by_id(asset_id):
    """Ambil aset berdasarkan ID"""
    try:
        response = supabase.table('assets').select('*').eq('id', asset_id).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]
        return None
    except:
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
            pass  # Ignore jika tidak ada data lama
        
        # Buat pending registration baru
        expires_at = (datetime.now() + timedelta(hours=1)).isoformat()
        data = {
            'email': email,
            'role': role,
            'token': token,
            'expires_at': expires_at
        }
        
        print(f"🔍 Attempting to insert: {data}")  # Debug log
        
        response = supabase.table('pending_registrations').insert(data).execute()
        
        print(f"✅ Response: {response}")  # Debug log
        
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"❌ Error create_pending_registration: {e}")
        import traceback
        traceback.print_exc()
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
    try:
        data = {
            'account_code': account_code,
            'account_name': account_name,
            'account_type': account_type,
            'normal_balance': normal_balance,
            'beginning_balance': float(beginning_balance)
        }
        response = supabase.table('accounts').insert(data).execute()
        return response.data[0] if response.data else None
    except:
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
        
        # Auto posting ke CRJ (Cash Receipt Journal)
        if response.data:
            # Debit Kas
            create_journal_entry(
                date=datetime.now().strftime('%Y-%m-%d'),
                account_code='1-1101',
                account_name='Kas',
                description=f'Penjualan {transaction_code}',
                debit=total_amount,
                credit=0,
                journal_type='CRJ',
                ref_code=transaction_code
            )
            # Credit Penjualan
            create_journal_entry(
                date=datetime.now().strftime('%Y-%m-%d'),
                account_code='4-1101',
                account_name='Penjualan',
                description=f'Penjualan {transaction_code}',
                debit=0,
                credit=total_amount,
                journal_type='CRJ',
                ref_code=transaction_code
            )
            
            # HPP (60% dari harga jual)
            total_hpp = sum(item['quantity'] * item['price'] * 0.6 for item in items)
            if total_hpp > 0:
                create_journal_entry(
                    date=datetime.now().strftime('%Y-%m-%d'),
                    account_code='5-1101',
                    account_name='Harga Pokok Penjualan',
                    description=f'HPP {transaction_code}',
                    debit=total_hpp,
                    credit=0,
                    journal_type='GJ',
                    ref_code=transaction_code
                )
                create_journal_entry(
                    date=datetime.now().strftime('%Y-%m-%d'),
                    account_code='1-1301',
                    account_name='Persediaan Ikan Mujair',
                    description=f'HPP {transaction_code}',
                    debit=0,
                    credit=total_hpp,
                    journal_type='GJ',
                    ref_code=transaction_code
                )
        
        return response.data[0] if response.data else None
    except Exception as e:
        print(f"Error create_transaction: {e}")
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

def create_purchase(item_type, item_name, quantity, unit_price, total_amount, employee_username, receipt_image=''):
    """Simpan pembelian dengan employee_username"""
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
        
        print(f"📦 Creating purchase: {data}")  # Debug log
        
        response = supabase.table('purchases').insert(data).execute()
        
        if response.data:
            print(f"✅ Purchase created: {response.data[0]}")
            purchase = response.data[0]
            ref_code = f"BL{datetime.now().strftime('%d%m')}{purchase['id']:03d}"
            date_str = datetime.now().strftime('%Y-%m-%d')
            
            # Auto posting ke CPJ (Cash Payment Journal)
            if item_type == 'peralatan':
                # Debit Peralatan (Aset Tetap)
                create_journal_entry(
                    date=date_str,
                    account_code='1-2101',
                    account_name='Peralatan',
                    description=f'Pembelian {item_name}',
                    debit=total_amount,
                    credit=0,
                    journal_type='CPJ',
                    ref_code=ref_code
                )
                # Credit Kas
                create_journal_entry(
                    date=date_str,
                    account_code='1-1101',
                    account_name='Kas',
                    description=f'Pembelian {item_name}',
                    debit=0,
                    credit=total_amount,
                    journal_type='CPJ',
                    ref_code=ref_code
                )
                
            elif item_type == 'perlengkapan':
                # Debit Perlengkapan (Aset Lancar)
                create_journal_entry(
                    date=date_str,
                    account_code='1-1401',
                    account_name='Perlengkapan',
                    description=f'Pembelian {item_name}',
                    debit=total_amount,
                    credit=0,
                    journal_type='CPJ',
                    ref_code=ref_code
                )
                # Credit Kas
                create_journal_entry(
                    date=date_str,
                    account_code='1-1101',
                    account_name='Kas',
                    description=f'Pembelian {item_name}',
                    debit=0,
                    credit=total_amount,
                    journal_type='CPJ',
                    ref_code=ref_code
                )
                
            elif item_type == 'bibit':
                # Debit Persediaan Ikan Mujair
                create_journal_entry(
                    date=date_str,
                    account_code='1-1301',
                    account_name='Persediaan Ikan Mujair',
                    description=f'Pembelian bibit {item_name}',
                    debit=total_amount,
                    credit=0,
                    journal_type='CPJ',
                    ref_code=ref_code
                )
                # Credit Kas
                create_journal_entry(
                    date=date_str,
                    account_code='1-1101',
                    account_name='Kas',
                    description=f'Pembelian bibit {item_name}',
                    debit=0,
                    credit=total_amount,
                    journal_type='CPJ',
                    ref_code=ref_code
                )
                
                # Update inventory card untuk bibit
                update_inventory(
                    item_name=item_name,
                    quantity=quantity,
                    unit_price=unit_price,
                    transaction_type='in',
                    ref_code=ref_code
                )
            
            return response.data[0]
        else:
            print(f"❌ No data returned from insert")
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
        
        query = supabase.table('journal_entries').select('*').eq('account_code', account_code)
        if end_date:
            query = query.lte('date', end_date)
        response = query.execute()
        entries = response.data if response.data else []
        
        balance = float(account.get('beginning_balance', 0))
        for entry in entries:
            if account['normal_balance'] == 'debit':
                balance += float(entry.get('debit', 0)) - float(entry.get('credit', 0))
            else:
                balance += float(entry.get('credit', 0)) - float(entry.get('debit', 0))
        
        return balance
    except:
        return 0

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
                    <h2>🛒 Catat Pembelian</h2>
                    <form method="POST">
                        <div class="form-row">
                            <div class="form-group">
                                <label>Jenis Item</label>
                                <select name="item_type" required>
                                    <option value="">-- Pilih --</option>
                                    <option value="bibit">Bibit Ikan</option>
                                    <option value="perlengkapan">Perlengkapan (Pakan, Obat, dll)</option>
                                    <option value="peralatan">Peralatan</option>
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Nama Item</label>
                                <input type="text" name="item_name" required placeholder="Nama barang">
                            </div>
                        </div>
                        <div class="form-row">
                            <div class="form-group">
                                <label>Jumlah</label>
                                <input type="number" name="quantity" step="0.01" min="0.01" required placeholder="0">
                            </div>
                            <div class="form-group">
                                <label>Harga Satuan</label>
                                <input type="text" name="unit_price" required placeholder="Rp0,00" id="unitPrice">
                            </div>
                        </div>
                        <button type="submit" class="btn-sm btn-success btn-block">💾 Simpan Pembelian</button>
                    </form>
                </div>
            </div>
        </div>
        <script>
        document.getElementById('unitPrice').addEventListener('blur', function() {{
            let val = this.value.replace(/[^0-9]/g, '');
            if (val) {{
                this.value = 'Rp' + parseInt(val).toLocaleString('id-ID') + ',00';
            }}
        }});
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
            ('crj', '💵', 'Jurnal CRJ', '/akuntan/journal-crj'),
            ('cpj', '💸', 'Jurnal CPJ', '/akuntan/journal-cpj'),
            ('sj', '📄', 'Jurnal SJ', '/akuntan/journal-sj'),
            ('pj', '🛒', 'Jurnal PJ', '/akuntan/journal-pj'),
            ('gj', '📝', 'Jurnal Umum', '/akuntan/journal-gj'),
            ('ledger', '📚', 'Buku Besar', '/akuntan/ledger'),
            ('trial', '⚖️', 'Neraca Saldo', '/akuntan/trial-balance'),
            ('adjustment', '🔧', 'Jurnal Penyesuaian', '/akuntan/adjustment-journal'),
            ('inventory', '📦', 'Inventory Card', '/akuntan/inventory'),
            ('assets', '🏢', 'Aset & Penyusutan', '/akuntan/assets'),
            ('financial', '📊', 'Laporan Keuangan', '/akuntan/financial-statements'),
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
                    <button class="btn-sm btn-warning" onclick="editTransaction('{trans['transaction_code']}')">✏️ Edit</button>
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
        
        function editTransaction(code) {{
            if (confirm('Fitur edit akan segera hadir!')) {{
                // TODO: Implement edit transaction
            }}
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
    """Hapus transaksi"""
    if 'username' not in session or session.get('role') != 'kasir':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        # Hapus transaksi
        supabase.table('transactions').delete().eq('transaction_code', transaction_code).execute()
        
        # Hapus jurnal entries terkait
        supabase.table('journal_entries').delete().eq('ref_code', transaction_code).execute()
        
        return jsonify({'success': True, 'message': 'Transaksi berhasil dihapus'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/kasir/daily-report')
def kasir_daily_report():
    """Laporan harian kasir"""
    if 'username' not in session or session.get('role') != 'kasir':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    today = datetime.now().strftime('%Y-%m-%d')
    
    transactions = get_transactions(start_date=today, end_date=today)
    total_sales = sum(float(t['total_amount']) for t in transactions)
    total_items = sum(sum(item['quantity'] for item in (json.loads(t['items']) if isinstance(t['items'], str) else t['items'])) for t in transactions)
    
    # Grafik penjualan per jam
    sales_by_hour = {}
    for trans in transactions:
        date_obj = datetime.fromisoformat(trans['date'].replace('Z', '+00:00'))
        hour = date_obj.hour
        sales_by_hour[hour] = sales_by_hour.get(hour, 0) + float(trans['total_amount'])
    
    chart_data = []
    for hour in range(24):
        chart_data.append({'hour': f"{hour:02d}:00", 'sales': sales_by_hour.get(hour, 0)})
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Laporan Harian - Geboy Mujair</title>
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
                    <li><a href="/kasir/daily-report" class="active"><span class="icon">📊</span> Laporan Harian</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Laporan Harian</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">📝</div>
                        <div class="stat-value">{len(transactions)}</div>
                        <div class="stat-label">Transaksi Hari Ini</div>
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
                    <h2>📊 Grafik Penjualan per Jam</h2>
                    <canvas id="salesChart" style="max-height: 400px;"></canvas>
                </div>
                
                <div class="content-section no-print">
                    <button onclick="window.print()" class="btn-sm btn-primary btn-block">🖨️ Cetak Laporan</button>
                </div>
            </div>
        </div>
        
        <script>
        const ctx = document.getElementById('salesChart').getContext('2d');
        const chartData = {json.dumps(chart_data)};
        
        new Chart(ctx, {{
            type: 'bar',
            data: {{
                labels: chartData.map(d => d.hour),
                datasets: [{{
                    label: 'Penjualan (Rp)',
                    data: chartData.map(d => d.sales),
                    backgroundColor: 'rgba(102, 126, 234, 0.6)',
                    borderColor: 'rgba(102, 126, 234, 1)',
                    borderWidth: 2
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
                                return 'Rp' + context.parsed.y.toLocaleString('id-ID');
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

@app.route('/karyawan/purchase', methods=['GET', 'POST'])
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

@app.route('/karyawan/purchase-history')
def karyawan_purchase_history():
    """Riwayat pembelian karyawan"""
    if 'username' not in session or session.get('role') != 'karyawan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Debug: tampilkan semua purchases dulu
    all_purchases = get_purchases()
    print(f"📋 All purchases: {len(all_purchases)}")
    for p in all_purchases:
        print(f"   - {p.get('employee_username')} : {p.get('item_name')}")
    
    # Filter berdasarkan username
    purchases = [p for p in all_purchases if p.get('employee_username') == username]
    print(f"📋 Filtered for '{username}': {len(purchases)}")
    
    # ... sisa kode
    
    purchases_html = ""
    for p in purchases:
        date_obj = datetime.fromisoformat(p['date'].replace('Z', '+00:00'))
        ref_code = f"BL{date_obj.strftime('%d%m')}{p['id']:03d}"
        
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
                <span style="background: #28a745; color: white; padding: 5px 10px; border-radius: 5px; font-size: 12px;">
                    ✓ {p.get('status', 'approved').upper()}
                </span>
            </td>
        </tr>
        """
    
    total_pembelian = sum(float(p['total_amount']) for p in purchases)
    
    html = fr"""
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
                                    <th class="text-center">Status</th>
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
                            """}
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html

@app.route('/akuntan/accounts', methods=['GET', 'POST'])
def akuntan_accounts():
    """Kelola daftar akun"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            account_code = request.form.get('account_code')
            account_name = request.form.get('account_name')
            account_type = request.form.get('account_type')
            normal_balance = request.form.get('normal_balance')
            beginning_balance = float(request.form.get('beginning_balance', 0))
            
            if create_account(account_code, account_name, account_type, normal_balance, beginning_balance):
                flash('Akun berhasil ditambahkan!', 'success')
            else:
                flash('Gagal menambahkan akun!', 'error')
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
    
    username = session.get('username', 'User')
    accounts = get_all_accounts()
    
    flash_html = ''.join([
        f'<div class="alert alert-{cat}">{msg}</div>'
        for cat, msg in session.pop('_flashes', [])
    ])
    
    # Kelompokkan akun berdasarkan tipe
    accounts_by_type = {
        'Aset': [a for a in accounts if a['account_code'].startswith('1-')],
        'Kewajiban': [a for a in accounts if a['account_code'].startswith('2-')],
        'Ekuitas': [a for a in accounts if a['account_code'].startswith('3-')],
        'Pendapatan': [a for a in accounts if a['account_code'].startswith('4-')],
        'Beban': [a for a in accounts if a['account_code'].startswith('5-') or a['account_code'].startswith('6-')]
    }
    
    accounts_html = ""
    for acc in accounts:
        balance = get_ledger_balance(acc['account_code'])
        accounts_html += f"""
        <tr>
            <td class="text-center"><strong>{acc['account_code']}</strong></td>
            <td>
                <input type="text" id="name_{acc['account_code'].replace('-', '_')}" 
                    value="{acc['account_name']}" 
                    style="border: none; background: transparent; width: 100%;">
            </td>
            <td class="text-center" style="text-transform: capitalize;">{acc['normal_balance']}</td>
            <td class="text-right">
                <input type="text" id="bal_{acc['account_code'].replace('-', '_')}" 
                    value="{format_rupiah(acc.get('beginning_balance', 0))}" 
                    style="border: none; background: transparent; width: 100%; text-align: right;">
            </td>
            <td class="text-right"><strong>{format_rupiah(balance)}</strong></td>
            <td class="text-center">
                <button class="btn-sm btn-warning" onclick="editAccount('{acc['account_code']}')">✏️</button>
                <button class="btn-sm btn-danger" onclick="deleteAccount('{acc['account_code']}')">🗑️</button>
            </td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daftar Akun - Geboy Mujair</title>
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
                    <li><a href="/akuntan/accounts" class="active"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/journal-crj"><span class="icon">💵</span> Jurnal CRJ</a></li>
                    <li><a href="/akuntan/journal-cpj"><span class="icon">💸</span> Jurnal CPJ</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> Neraca Saldo</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Daftar Akun (Chart of Accounts)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <h2>➕ Tambah Akun Baru</h2>
                    {flash_html}
                    <form method="POST">
                        <div class="form-row">
                            <div class="form-group">
                                <label>Kode Akun *</label>
                                <input type="text" name="account_code" required placeholder="1-1101" pattern="[0-9]-[0-9]{4}">
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
                                <input type="text" name="beginning_balance" placeholder="Rp0,00" id="beginningBalance">
                            </div>
                        </div>
                        
                        <button type="submit" class="btn-sm btn-success btn-block">💾 Tambah Akun</button>
                    </form>
                </div>
                
                <div class="content-section">
                    <h2>📋 Chart of Accounts</h2>
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
        
        <script>
        document.getElementById('beginningBalance').addEventListener('blur', function(){{
            let val = this.value.replace(/[^0-9]/g, '');
            if (val) {{
                this.value = 'Rp' + parseInt(val).toLocaleString('id-ID') + ',00';
            }}
        }});
        
        function formatRupiah(amount) {{
            return 'Rp' + parseInt(amount).toLocaleString('id-ID') + ',00';
        }}
        
        function editAccount(code) {{
            const safeCode = code.replace(/-/g, '_');
            const nameEl = document.getElementById('name_' + safeCode);
            const balEl = document.getElementById('bal_' + safeCode);
            
            const name = nameEl ? nameEl.value : '';
            let balance = balEl ? balEl.value : '';
            
            // Konversi format Rupiah ke angka
            balance = balance.replace(/[^\d]/g, '');
            
            const form = document.createElement('form');
            form.method = 'POST';
            form.action = '/akuntan/accounts/edit/' + encodeURIComponent(code);
            
            const nameInput = document.createElement('input');
            nameInput.type = 'hidden';
            nameInput.name = 'account_name';
            nameInput.value = name;
            
            const balanceInput = document.createElement('input');
            balanceInput.type = 'hidden';
            balanceInput.name = 'beginning_balance';
            balanceInput.value = balance || '0';
            
            form.appendChild(nameInput);
            form.appendChild(balanceInput);
            document.body.appendChild(form);
            form.submit();
        }}
        
        function deleteAccount(code) {{
            if (!confirm('Yakin hapus akun ' + code + '?')) return;
            
            fetch('/akuntan/accounts/delete/' + encodeURIComponent(code), {{
                method: 'DELETE'
            }})
            .then(res => res.json())
            .then(data => {{
                alert(data.message || 'Akun berhasil dihapus');
                if (data.success) location.reload();
            }})
            .catch(err => {{
                console.error(err);
                alert('Terjadi error saat menghapus akun');
            }});
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
            document.getElementById('datetime').textContent = now.toLocaleDateString('id-ID', options);
        }}
        setInterval(updateDateTime, 1000);
        updateDateTime();
        </script>
    </body>
    </html>
    """ 
    return html

@app.route('/akuntan/accounts/edit/<account_code>', methods=['POST'])
def akuntan_edit_account(account_code):
    """Edit akun"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        account_name = request.form.get('account_name')
        beginning_balance = parse_rupiah(request.form.get('beginning_balance', '0'))
        
        # Update account
        data = {
            'account_name': account_name,
            'beginning_balance': float(beginning_balance)
        }
        response = supabase.table('accounts').update(data).eq('account_code', account_code).execute()
        
        if response.data:
            flash('Akun berhasil diupdate!', 'success')
        else:
            flash('Gagal update akun!', 'error')
    except Exception as e:
        flash(f'Error: {str(e)}', 'error')
    
    return redirect(url_for('akuntan_accounts'))

@app.route('/akuntan/accounts/delete/<account_code>', methods=['DELETE'])
def akuntan_delete_account(account_code):
    """Hapus akun"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return jsonify({'success': False, 'message': 'Unauthorized'})
    
    try:
        # Cek apakah akun sudah digunakan di jurnal
        journals = get_journal_entries()
        used = any(j['account_code'] == account_code for j in journals)
        
        if used:
            return jsonify({'success': False, 'message': 'Akun sudah digunakan di jurnal, tidak bisa dihapus!'})
        
        supabase.table('accounts').delete().eq('account_code', account_code).execute()
        return jsonify({'success': True, 'message': 'Akun berhasil dihapus'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@app.route('/akuntan/journal-crj')
def akuntan_journal_crj():
    """Jurnal Penerimaan Kas (Cash Receipt Journal)"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    journals = get_journal_entries(journal_type='CRJ')
    
    # Hitung rekapitulasi
    total_debit = sum(float(j.get('debit', 0)) for j in journals)
    total_credit = sum(float(j.get('credit', 0)) for j in journals)
    
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
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Jurnal CRJ - Geboy Mujair</title>
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
                    <li><a href="/akuntan/journal-crj" class="active"><span class="icon">💵</span> Jurnal CRJ</a></li>
                    <li><a href="/akuntan/journal-cpj"><span class="icon">💸</span> Jurnal CPJ</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> Neraca Saldo</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Jurnal Penerimaan Kas (CRJ)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <h2>💵 Cash Receipt Journal</h2>
                    <p style="margin-bottom: 20px; color: #666;">
                        Jurnal khusus untuk mencatat semua penerimaan kas dari penjualan tunai dan transaksi lainnya.
                    </p>
                    
                    <div style="overflow-x: auto;">
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
                                {journals_html if journals_html else '<tr><td colspan="7" class="text-center">Belum ada transaksi</td></tr>'}
                            </tbody>
                            {f'''
                            <tfoot style="background: #f8f9fa; font-weight: bold;">
                                <tr>
                                    <td colspan="5" class="text-right" style="padding: 15px;">REKAPITULASI CRJ:</td>
                                    <td class="text-right" style="padding: 15px; color: #667eea;">{format_rupiah(total_debit)}</td>
                                    <td class="text-right" style="padding: 15px; color: #dc3545;">{format_rupiah(total_credit)}</td>
                                </tr>
                            </tfoot>
                            ''' if journals else ''}
                        </table>
                    </div>
                </div>
                
                <div class="content-section no-print" style="background: #f8f9fa; border-left: 4px solid #28a745;">
                    <h3 style="color: #28a745; margin-bottom: 10px;">ℹ️ Tentang Jurnal CRJ</h3><p style="line-height: 1.8;">
                        Jurnal Penerimaan Kas (Cash Receipt Journal) digunakan untuk mencatat semua transaksi penerimaan kas. 
                        Transaksi dari POS kasir akan otomatis tercatat di sini dengan:<br>
                        • <strong>Debit:</strong> Kas (1-1101)<br>
                        • <strong>Kredit:</strong> Penjualan (4-1101)<br><br>
                        Rekapitulasi dari jurnal ini akan diposting ke Buku Besar secara otomatis.
                    </p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html

@app.route('/akuntan/journal-cpj')
def akuntan_journal_cpj():
    """Jurnal Pengeluaran Kas (Cash Payment Journal)"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    journals = get_journal_entries(journal_type='CPJ')
    
    total_debit = sum(float(j.get('debit', 0)) for j in journals)
    total_credit = sum(float(j.get('credit', 0)) for j in journals)
    
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
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Jurnal CPJ - Geboy Mujair</title>
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
                    <li><a href="/akuntan/journal-crj"><span class="icon">💵</span> Jurnal CRJ</a></li>
                    <li><a href="/akuntan/journal-cpj" class="active"><span class="icon">💸</span> Jurnal CPJ</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> Neraca Saldo</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Jurnal Pengeluaran Kas (CPJ)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <h2>💸 Cash Payment Journal</h2>
                    <p style="margin-bottom: 20px; color: #666;">
                        Jurnal khusus untuk mencatat semua pengeluaran kas untuk pembelian dan pembayaran lainnya.
                    </p>
                    
                    <div style="overflow-x: auto;">
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
                                {journals_html if journals_html else '<tr><td colspan="7" class="text-center">Belum ada transaksi</td></tr>'}
                            </tbody>
                            {f'''
                            <tfoot style="background: #f8f9fa; font-weight: bold;">
                                <tr>
                                    <td colspan="5" class="text-right" style="padding: 15px;">REKAPITULASI CPJ:</td>
                                    <td class="text-right" style="padding: 15px; color: #667eea;">{format_rupiah(total_debit)}</td>
                                    <td class="text-right" style="padding: 15px; color: #dc3545;">{format_rupiah(total_credit)}</td>
                                </tr>
                            </tfoot>
                            ''' if journals else ''}
                        </table>
                    </div>
                </div>
                
                <div class="content-section no-print" style="background: #f8f9fa; border-left: 4px solid #dc3545;">
                    <h3 style="color: #dc3545; margin-bottom: 10px;">ℹ️ Tentang Jurnal CPJ</h3>
                    <p style="line-height: 1.8;">
                        Jurnal Pengeluaran Kas (Cash Payment Journal) digunakan untuk mencatat semua transaksi pengeluaran kas. 
                        Transaksi pembelian dari karyawan akan otomatis tercatat di sini dengan:<br>
                        • <strong>Debit:</strong> Peralatan/Perlengkapan/Persediaan<br>
                        • <strong>Kredit:</strong> Kas (1-1101)<br><br>
                        Rekapitulasi dari jurnal ini akan diposting ke Buku Besar secara otomatis.
                    </p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html

@app.route('/akuntan/journal-gj', methods=['GET', 'POST'])
def akuntan_journal_gj():
    """Jurnal Umum (General Journal)"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        try:
            date = request.form.get('date')
            account_code = request.form.get('account_code')
            description = request.form.get('description')
            debit = parse_rupiah(request.form.get('debit', '0'))
            credit = parse_rupiah(request.form.get('credit', '0'))
            ref_code = request.form.get('ref_code', 'MANUAL')
            
            # Validasi
            if debit == 0 and credit == 0:
                flash('Minimal isi debit atau kredit!', 'error')
            elif debit > 0 and credit > 0:
                flash('Tidak boleh mengisi debit dan kredit bersamaan!', 'error')
            else:
                # Ambil nama akun
                accounts = get_all_accounts()
                account = next((a for a in accounts if a['account_code'] == account_code), None)
                
                if account:
                    if create_journal_entry(date, account_code, account['account_name'], description, debit, credit, 'GJ', ref_code):
                        flash('Jurnal berhasil ditambahkan!', 'success')
                    else:
                        flash('Gagal menambahkan jurnal!', 'error')
                else:
                    flash('Kode akun tidak ditemukan!', 'error')
        except Exception as e:
            flash(f'Error: {str(e)}', 'error')
    
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
    
    accounts_options = "".join([f'<option value="{a["account_code"]}">{a["account_code"]} - {a["account_name"]}</option>' for a in accounts])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Jurnal Umum - Geboy Mujair</title>
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
                    <li><a href="/akuntan/journal-crj"><span class="icon">💵</span> Jurnal CRJ</a></li>
                    <li><a href="/akuntan/journal-cpj"><span class="icon">💸</span> Jurnal CPJ</a></li>
                    <li><a href="/akuntan/journal-gj" class="active"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> Neraca Saldo</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Jurnal Umum (General Journal)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <h2>➕ Tambah Entry Jurnal</h2>
                    {flash_html}
                    <form method="POST">
                        <div class="form-row">
                            <div class="form-group">
                                <label>Tanggal *</label>
                                <input type="date" name="date" required value="{datetime.now().strftime('%Y-%m-%d')}">
                            </div>
                            <div class="form-group">
                                <label>Akun *</label>
                                <select name="account_code" required>
                                    <option value="">-- Pilih Akun --</option>
                                    {accounts_options}
                                </select>
                            </div>
                            <div class="form-group">
                                <label>Ref Code</label>
                                <input type="text" name="ref_code" placeholder="MANUAL" value="MANUAL">
                            </div>
                        </div>
                        
                        <div class="form-group">
                            <label>Keterangan *</label>
                            <textarea name="description" required rows="2" placeholder="Deskripsi transaksi..."></textarea>
                        </div>
                        
                        <div class="form-row">
                            <div class="form-group">
                                <label>Debit</label>
                                <input type="text" name="debit" placeholder="Rp0,00" id="debitInput">
                            </div>
                            <div class="form-group">
                                <label>Kredit</label>
                                <input type="text" name="credit" placeholder="Rp0,00" id="creditInput">
                            </div>
                        </div>
                        
                        <button type="submit" class="btn-sm btn-success btn-block">💾 Simpan Entry</button>
                    </form>
                </div>
                
                <div class="content-section">
                    <h2>📝 General Journal</h2>
                    <div style="overflow-x: auto;">
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
                                {journals_html if journals_html else '<tr><td colspan="7" class="text-center">Belum ada entry</td></tr>'}
                            </tbody>
                            {f'''
                            <tfoot style="background: #f8f9fa; font-weight: bold;">
                                <tr>
                                    <td colspan="5" class="text-right" style="padding: 15px;">TOTAL:</td>
                                    <td class="text-right" style="padding: 15px; color: #667eea;">{format_rupiah(total_debit)}</td>
                                    <td class="text-right" style="padding: 15px; color: #dc3545;">{format_rupiah(total_credit)}</td>
                                </tr>
                                <tr style="background: {'#d4edda' if abs(total_debit - total_credit) < 0.01 else '#f8d7da'};">
                                    <td colspan="5" class="text-right" style="padding: 15px;">STATUS:</td>
                                    <td colspan="2" class="text-center" style="padding: 15px;">
                                        {'✓ BALANCE' if abs(total_debit - total_credit) < 0.01 else '✗ NOT BALANCE'}
                                    </td>
                                </tr>
                            </tfoot>
                            ''' if journals else ''}
                        </table>
                    </div>
                </div>
            </div>
        </div>
        
        <script>
        // Format rupiah
        ['debitInput', 'creditInput'].forEach(id => {{
            document.getElementById(id).addEventListener('blur', function() {{
                let val = this.value.replace(/[^0-9]/g, '');
                if (val) {{
                    this.value = 'Rp' + parseInt(val).toLocaleString('id-ID') + ',00';
                }}
            }});
        }});
        
        // Auto clear salah satu jika yang lain diisi
        document.getElementById('debitInput').addEventListener('input', function() {{
            if (this.value) {{
                document.getElementById('creditInput').value = '';
            }}
        }});
        
        document.getElementById('creditInput').addEventListener('input', function() {{
            if (this.value) {{
                document.getElementById('debitInput').value = '';
            }}
        }});
        </script>
    </body>
    </html>
    """
    
    return html

@app.route('/akuntan/ledger')
def akuntan_ledger():
    """Buku Besar (General Ledger)"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    accounts = get_all_accounts()
    
    selected_account = request.args.get('account_code', '')
    
    ledger_html = ""
    if selected_account:
        account = next((a for a in accounts if a['account_code'] == selected_account), None)
        if account:
            entries = [e for e in get_journal_entries() if e['account_code'] == selected_account]
            
            balance = float(account.get('beginning_balance', 0))
            
            ledger_html = f"""
            <div style="background: #667eea; color: white; padding: 15px; border-radius: 10px 10px 0 0; margin-bottom: 0;">
                <h3 style="margin: 0;">Buku Besar: {account['account_name']}</h3>
                <p style="margin: 5px 0 0 0; font-size: 14px;">Kode: {account['account_code']} | Saldo Normal: {account['normal_balance'].title()}</p>
            </div>
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
                    <tr style="background: #f8f9fa; font-weight: bold;">
                        <td colspan="5">Saldo Awal</td>
                        <td class="text-right">{format_rupiah(balance)}</td>
                    </tr>
            """
            
            for entry in entries:
                debit = float(entry.get('debit', 0))
                credit = float(entry.get('credit', 0))
                
                if account['normal_balance'] == 'debit':
                    balance += debit - credit
                else:
                    balance += credit - debit
                
                ledger_html += f"""
                    <tr>
                        <td>{entry['date']}</td>
                        <td>{entry['description']}</td>
                        <td class="text-center">{entry.get('ref_code', '-')}</td>
                        <td class="text-right">{format_rupiah(debit)}</td>
                        <td class="text-right">{format_rupiah(credit)}</td>
                        <td class="text-right"><strong>{format_rupiah(balance)}</strong></td>
                    </tr>
                """
            
            ledger_html += f"""
                    <tr style="background: #667eea; color: white; font-weight: bold;">
                        <td colspan="5" class="text-right" style="padding: 15px;">SALDO AKHIR:</td>
                        <td class="text-right" style="padding: 15px; font-size: 18px;">{format_rupiah(balance)}</td>
                    </tr>
                </tbody>
            </table>
            """
    
    accounts_options = "".join([
        f'<option value="{a["account_code"]}" {"selected" if a["account_code"] == selected_account else ""}>{a["account_code"]} - {a["account_name"]}</option>' 
        for a in accounts
    ])
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Buku Besar - Geboy Mujair</title>
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
                    <li><a href="/akuntan/journal-crj"><span class="icon">💵</span> Jurnal CRJ</a></li>
                    <li><a href="/akuntan/journal-cpj"><span class="icon">💸</span> Jurnal CPJ</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/ledger" class="active"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> Neraca Saldo</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Buku Besar (General Ledger)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <h2>🔍 Pilih Akun</h2>
                    <form method="GET">
                        <div class="form-row">
                            <div class="form-group">
                                <label>Akun *</label>
                                <select name="account_code" required onchange="this.form.submit()">
                                    <option value="">-- Pilih Akun untuk Melihat Buku Besar --</option>
                                    {accounts_options}
                                </select>
                            </div>
                        </div>
                    </form>
                </div>
                
                {f'<div class="content-section">{ledger_html}</div>' if ledger_html else ''}
                
                {'' if selected_account else '''
                <div class="content-section" style="text-align: center; padding: 60px 20px;">
                    <div style="font-size: 60px; margin-bottom: 20px;">📚</div>
                    <h3 style="color: #666; margin-bottom: 10px;">Pilih Akun untuk Melihat Buku Besar</h3>
                    <p style="color: #999;">Pilih akun dari dropdown di atas untuk menampilkan detail buku besar</p>
                </div>
                '''}
            </div>
        </div>
    </body>
    </html>
    """
    
    return html

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
                    <li><a href="/akuntan/journal-crj"><span class="icon">💵</span> Jurnal CRJ</a></li>
                    <li><a href="/akuntan/journal-cpj"><span class="icon">💸</span> Jurnal CPJ</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance" class="active"><span class="icon">⚖️</span> Neraca Saldo</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
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

@app.route('/akuntan/financial-statements')
def akuntan_financial_statements():
    """Laporan Keuangan"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Generate laporan laba rugi dan neraca
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = datetime.now().replace(day=1).strftime('%Y-%m-%d')
    
    income_statement = generate_income_statement(start_date, end_date)
    balance_sheet = generate_balance_sheet(end_date)
    
    # Laporan Laba Rugi
    revenue_html = ""
    if income_statement:
        for item in income_statement['revenue_details']:
            if item['amount'] > 0:
                revenue_html += f"""
                <tr>
                    <td style="padding-left: 30px;">{item['account_name']}</td>
                    <td class="text-right">{format_rupiah(item['amount'])}</td>
                </tr>
                """
    
    expense_html = ""
    if income_statement:
        for item in income_statement['expense_details']:
            if item['amount'] > 0:
                expense_html += f"""
                <tr>
                    <td style="padding-left: 30px;">{item['account_name']}</td>
                    <td class="text-right">{format_rupiah(item['amount'])}</td>
                </tr>
                """
    
    # Neraca
    asset_html = ""
    if balance_sheet:
        for item in balance_sheet['asset_details']:
            if item['amount'] != 0:
                asset_html += f"""
                <tr>
                    <td style="padding-left: 30px;">{item['account_name']}</td>
                    <td class="text-right">{format_rupiah(item['amount'])}</td>
                </tr>
                """
    
    liability_html = ""
    if balance_sheet:
        for item in balance_sheet['liability_details']:
            if item['amount'] != 0:
                liability_html += f"""
                <tr>
                    <td style="padding-left: 30px;">{item['account_name']}</td>
                    <td class="text-right">{format_rupiah(item['amount'])}</td>
                </tr>
                """
    
    equity_html = ""
    if balance_sheet:
        for item in balance_sheet['equity_details']:
            if item['amount'] != 0:
                equity_html += f"""
                <tr>
                    <td style="padding-left: 30px;">{item['account_name']}</td>
                    <td class="text-right">{format_rupiah(item['amount'])}</td>
                </tr>
                """
    
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
                    <li><a href="/akuntan/journal-crj"><span class="icon">💵</span> Jurnal CRJ</a></li>
                    <li><a href="/akuntan/journal-cpj"><span class="icon">💸</span> Jurnal CPJ</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> Neraca Saldo</a></li>
                    <li><a href="/akuntan/financial-statements" class="active"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Laporan Keuangan</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <!-- LAPORAN LABA RUGI -->
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
                                <td class="text-right">{format_rupiah(income_statement['revenue'] if income_statement else 0)}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <tr style="background: #667eea; color: white;">
                                <td colspan="2" style="padding: 12px; font-weight: bold;">BEBAN</td>
                            </tr>
                            {expense_html if expense_html else '<tr><td colspan="2" style="padding-left: 30px; color: #999;">Tidak ada beban</td></tr>'}
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding-left: 30px;">Total Beban</td>
                                <td class="text-right">{format_rupiah(income_statement['expenses'] if income_statement else 0)}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <tr style="background: {'#d4edda' if income_statement and income_statement['net_income'] >= 0 else '#f8d7da'}; font-weight: bold; font-size: 18px;">
                                <td style="padding: 15px;">LABA (RUGI) BERSIH</td>
                                <td class="text-right" style="padding: 15px; color: {'#155724' if income_statement and income_statement['net_income'] >= 0 else '#721c24'};">
                                    {format_rupiah(income_statement['net_income'] if income_statement else 0)}
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                
                <!-- NERACA -->
                <div class="content-section">
                    <div style="text-align: center; margin-bottom: 30px;">
                        <h2 style="color: #667eea; margin-bottom: 5px;">GEBOY MUJAIR</h2>
                        <h3 style="color: #333; margin-bottom: 5px;">NERACA</h3>
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
                                <td class="text-right">{format_rupiah(balance_sheet['assets'] if balance_sheet else 0)}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <tr style="background: #667eea; color: white;">
                                <td colspan="2" style="padding: 12px; font-weight: bold;">KEWAJIBAN</td>
                            </tr>
                            {liability_html if liability_html else '<tr><td colspan="2" style="padding-left: 30px; color: #999;">Tidak ada kewajiban</td></tr>'}
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding-left: 30px;">Total Kewajiban</td>
                                <td class="text-right">{format_rupiah(balance_sheet['liabilities'] if balance_sheet else 0)}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <tr style="background: #667eea; color: white;">
                                <td colspan="2" style="padding: 12px; font-weight: bold;">EKUITAS</td>
                            </tr>
                            {equity_html if equity_html else '<tr><td colspan="2" style="padding-left: 30px; color: #999;">Tidak ada ekuitas</td></tr>'}
                            <tr style="background: #f8f9fa; font-weight: bold;">
                                <td style="padding-left: 30px;">Total Ekuitas</td>
                                <td class="text-right">{format_rupiah(balance_sheet['equity'] if balance_sheet else 0)}</td>
                            </tr>
                            
                            <tr style="height: 20px;"><td colspan="2"></td></tr>
                            
                            <tr style="background: #667eea; color: white; font-weight: bold; font-size: 18px;">
                                <td style="padding: 15px;">TOTAL KEWAJIBAN & EKUITAS</td>
                                <td class="text-right" style="padding: 15px;">
                                    {format_rupiah((balance_sheet['liabilities'] + balance_sheet['equity']) if balance_sheet else 0)}
                                </td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                
                <div class="content-section no-print">
                    <button onclick="window.print()" class="btn-sm btn-primary btn-block">🖨️ Cetak Laporan Keuangan</button>
                </div>
            </div>
        </div>
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
    """Laporan keuangan untuk owner (read-only)"""
    if 'username' not in session or session.get('role') != 'owner':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Sama seperti akuntan tapi read-only
    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = datetime.now().replace(day=1).strftime('%Y-%m-%d')
    
    income_statement = generate_income_statement(start_date, end_date)
    balance_sheet = generate_balance_sheet(end_date)
    
    # (HTML sama seperti akuntan_financial_statements, tapi sidebar untuk owner)
    # Untuk menghemat ruang, saya skip detail HTML-nya karena sama
    
    return akuntan_financial_statements().replace('Akuntan', 'Owner').replace('/dashboard/akuntan', '/dashboard/owner')

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
                    <li><a href="/akuntan/journal-crj"><span class="icon">💵</span> Jurnal CRJ</a></li>
                    <li><a href="/akuntan/journal-cpj"><span class="icon">💸</span> Jurnal CPJ</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/adjustment-journal" class="active"><span class="icon">🔧</span> Jurnal Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Jurnal Penutup</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Jurnal Pembalik</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> Neraca Saldo</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
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
            net_income = get_ledger_balance('3-9901')
            if net_income != 0:
                if net_income > 0:  # Laba
                    create_closing_entry(date, '3-9901', 'Ikhtisar Laba Rugi', 
                                       'Penutupan Laba ke Modal', net_income, 0)
                    create_closing_entry(date, '3-1101', 'Modal', 
                                       'Penutupan Laba ke Modal', 0, net_income)
                else:  # Rugi
                    create_closing_entry(date, '3-1101', 'Modal', 
                                       'Penutupan Rugi ke Modal', abs(net_income), 0)
                    create_closing_entry(date, '3-9901', 'Ikhtisar Laba Rugi', 
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
                    <li><a href="/akuntan/journal-crj"><span class="icon">💵</span> Jurnal CRJ</a></li>
                    <li><a href="/akuntan/journal-cpj"><span class="icon">💸</span> Jurnal CPJ</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Jurnal Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal" class="active"><span class="icon">🔒</span> Jurnal Penutup</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Jurnal Pembalik</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> Neraca Saldo</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
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
            ('1-1101', 'Kas', 'aset', 'debit', 5000000),
            ('1-1201', 'Piutang Usaha', 'aset', 'debit', 0),
            ('1-1301', 'Persediaan Ikan Mujair', 'aset', 'debit', 10000000),
            ('1-1401', 'Perlengkapan', 'aset', 'debit', 1000000),
            ('1-2101', 'Peralatan', 'aset', 'debit', 15000000),
            ('1-2102', 'Akumulasi Penyusutan Peralatan', 'aset', 'credit', 0),
            
            # KEWAJIBAN (2-xxxx)
            ('2-1101', 'Utang Usaha', 'kewajiban', 'credit', 0),
            ('2-1201', 'Utang Bank', 'kewajiban', 'credit', 0),
            
            # EKUITAS (3-xxxx)
            ('3-1101', 'Modal', 'ekuitas', 'credit', 31000000),
            ('3-1201', 'Prive', 'ekuitas', 'debit', 0),
            ('3-9901', 'Ikhtisar Laba Rugi', 'ekuitas', 'credit', 0),
            
            # PENDAPATAN (4-xxxx)
            ('4-1101', 'Penjualan', 'pendapatan', 'credit', 0),
            ('4-1201', 'Pendapatan Lain-lain', 'pendapatan', 'credit', 0),
            
            # HARGA POKOK PENJUALAN (5-xxxx)
            ('5-1101', 'Harga Pokok Penjualan', 'beban', 'debit', 0),
            
            # BEBAN (6-xxxx)
            ('6-1101', 'Beban Gaji', 'beban', 'debit', 0),
            ('6-1201', 'Beban Listrik', 'beban', 'debit', 0),
            ('6-1301', 'Beban Perlengkapan', 'beban', 'debit', 0),
            ('6-1401', 'Beban Penyusutan Peralatan', 'beban', 'debit', 0),
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
                    <li><a href="/akuntan/journal-crj"><span class="icon">💵</span> Jurnal CRJ</a></li>
                    <li><a href="/akuntan/journal-cpj"><span class="icon">💸</span> Jurnal CPJ</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Jurnal Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Jurnal Penutup</a></li>
                    <li><a href="/akuntan/reversing-journal" class="active"><span class="icon">🔄</span> Jurnal Pembalik</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> Neraca Saldo</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
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

# ============== ROUTES TAMBAHAN UNTUK INVENTORY & ASET ==============
@app.route('/akuntan/inventory', methods=['GET', 'POST'])
def akuntan_inventory():
    """Kelola Inventory - Kartu Persediaan"""
    if 'username' not in session or session.get('role') != 'akuntan':
        return redirect(url_for('login'))
    
    username = session.get('username', 'User')
    
    # Pilih metode
    method = request.args.get('method', 'fifo')
    
    inventory_card = get_inventory_card()
    
    # Hitung saldo per item
    items_summary = {}
    for card in inventory_card:
        item = card['item_name']
        if item not in items_summary:
            items_summary[item] = {
                'in_qty': 0, 
                'out_qty': 0, 
                'qty': 0, 
                'value': 0, 
                'transactions': []
            }
        
        if card['transaction_type'] == 'in':
            items_summary[item]['in_qty'] += float(card['quantity'])
            items_summary[item]['qty'] += float(card['quantity'])
            items_summary[item]['value'] += float(card['quantity']) * float(card['unit_price'])
        else:  # out
            items_summary[item]['out_qty'] += float(card['quantity'])
            items_summary[item]['qty'] -= float(card['quantity'])
            items_summary[item]['value'] -= float(card['quantity']) * float(card['unit_price'])
        
        items_summary[item]['transactions'].append(card)
    
    inventory_html = ""
    for item, data in items_summary.items():
        avg_price = data['value'] / data['qty'] if data['qty'] > 0 else 0
        inventory_html += f"""
        <tr>
            <td>🐟 {item}</td>
            <td class="text-center">{data['in_qty']:.2f} kg</td>
            <td class="text-center">{data['out_qty']:.2f} kg</td>
            <td class="text-center"><strong>{data['qty']:.2f} kg</strong></td>
            <td class="text-right">{format_rupiah(avg_price)}</td>
            <td class="text-right"><strong>{format_rupiah(data['value'])}</strong></td>
            <td class="text-center">
                <a href="/akuntan/inventory-detail?item={item}" class="btn-sm btn-info">📋 Detail</a>
            </td>
        </tr>
        """
    
    html = f"""
    <!DOCTYPE html>
    <html lang="id">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Inventory - Geboy Mujair</title>
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
                    <li><a href="/akuntan/inventory" class="active"><span class="icon">📦</span> Inventory</a></li>
                    <li><a href="/akuntan/assets"><span class="icon">🏢</span> Aset & Penyusutan</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Kartu Persediaan (Inventory Card)</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="content-section">
                    <h2>📦 Ringkasan Persediaan</h2>
                    <div class="form-group">
                        <label>Metode Penilaian Persediaan</label>
                        <select onchange="window.location.href='/akuntan/inventory?method=' + this.value">
                            <option value="fifo" {"selected" if method == "fifo" else ""}>FIFO (First In First Out)</option>
                            <option value="average" {"selected" if method == "average" else ""}>Average (Rata-rata Tertimbang)</option>
                        </select>
                    </div>
                    
                    <table>
                        <thead>
                            <tr>
                                <th>Nama Item</th>
                                <th class="text-center">IN (Masuk)</th>
                                <th class="text-center">OUT (Keluar)</th>
                                <th class="text-center">Balance (Saldo)</th>
                                <th class="text-right">Harga Rata-rata</th>
                                <th class="text-right">Nilai Total</th>
                                <th class="text-center">Aksi</th>
                            </tr>
                        </thead>
                        <tbody>
                            {inventory_html if inventory_html else '<tr><td colspan="7" class="text-center">Tidak ada persediaan</td></tr>'}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    
    return html

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
                    flash('Aset berhasil ditambahkan!', 'success')
                else:
                    flash('Gagal menambahkan aset!', 'error')
            except Exception as e:
                flash(f'Error: {str(e)}', 'error')
        
        elif action == 'calculate_depreciation':
            try:
                asset_id = int(request.form.get('asset_id'))
                period_year = int(request.form.get('period_year', 1))
                
                asset = get_asset_by_id(asset_id)
                if asset:
                    depreciation = calculate_depreciation(asset, period_year)
                    flash(f'Penyusutan tahun {period_year}: {format_rupiah(depreciation)}', 'success')
                else:
                    flash('Aset tidak ditemukan!', 'error')
            except Exception as e:
                flash(f'Error: {str(e)}', 'error')
        
        elif action == 'record_depreciation':
            try:
                asset_id = int(request.form.get('asset_id'))
                period_year = int(request.form.get('period_year', 1))
                period_date_str = request.form.get('period_date')
                period_date = datetime.strptime(period_date_str, '%Y-%m-%d')
                
                asset = get_asset_by_id(asset_id)
                if asset:
                    depreciation = calculate_depreciation(asset, period_year)
                    if record_depreciation_entry(asset, depreciation, period_date):
                        flash(f'Jurnal penyusutan berhasil dicatat: {format_rupiah(depreciation)}', 'success')
                    else:
                        flash('Gagal mencatat jurnal penyusutan!', 'error')
                else:
                    flash('Aset tidak ditemukan!', 'error')
            except Exception as e:
                flash(f'Error: {str(e)}', 'error')
        
        return redirect(url_for('akuntan_assets'))
    
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
        
        assets_html += f"""
        <tr>
            <td><strong>{asset['asset_code']}</strong></td>
            <td>{asset['asset_name']}</td>
            <td class="text-right">{format_rupiah(asset['cost'])}</td>
            <td class="text-center">{asset['useful_life']} tahun</td>
            <td class="text-center" style="text-transform: capitalize;">
                {asset['depreciation_method'].replace('_', ' ')}
            </td>
            <td class="text-right">{format_rupiah(asset.get('accumulated_depreciation', 0))}</td>
            <td class="text-right"><strong>{format_rupiah(asset.get('book_value', asset['cost']))}</strong></td>
            <td class="text-center">
                <button class="btn-sm btn-info" onclick="showDepreciationModal({asset['id']}, '{asset['asset_name']}', {years_used + 1})">
                    📊 Hitung
                </button>
                <button class="btn-sm btn-success" onclick="showRecordModal({asset['id']}, '{asset['asset_name']}', {years_used + 1})">
                    💾 Catat
                </button>
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

                <div class="form-group">
                    <label>Periode Penyusutan</label>
                    <select name="period_type" id="period_type">
                        <option value="annual">Per Tahun</option>
                        <option value="monthly">Per Bulan</option>
                    </select>
                </div>
                
                <ul class="sidebar-menu">
                    <li><a href="/dashboard/akuntan"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/akuntan/accounts"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/inventory"><span class="icon">📦</span> Inventory</a></li>
                    <li><a href="/akuntan/assets" class="active"><span class="icon">🏢</span> Aset & Penyusutan</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Aset Tetap & Penyusutan</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
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
                
                <div class="content-section" style="background: #f8f9fa; border-left: 4px solid #667eea;">
                    <h3 style="color: #667eea; margin-bottom: 15px;">📘 Penjelasan Metode Penyusutan</h3>
                    
                    <div style="margin-bottom: 20px;">
                        <h4 style="color: #333; margin-bottom: 10px;">1. Garis Lurus (Straight Line)</h4>
                        <p style="line-height: 1.8; margin-bottom: 5px;">
                            <strong>Formula:</strong> (Harga Perolehan - Nilai Residu) / Umur Ekonomis<br>
                            <strong>Karakteristik:</strong> Penyusutan sama setiap periode<br>
                            <strong>Contoh:</strong> Aset Rp10.000.000, Residu Rp1.000.000, Umur 5 tahun<br>
                            → Penyusutan per tahun = (10.000.000 - 1.000.000) / 5 = <strong>Rp1.800.000</strong>
                        </p>
                    </div>
                    
                    <div style="margin-bottom: 20px;">
                        <h4 style="color: #333; margin-bottom: 10px;">2. Saldo Menurun (Declining Balance)</h4>
                        <p style="line-height: 1.8; margin-bottom: 5px;">
                            <strong>Formula:</strong> Nilai Buku × (2 / Umur Ekonomis)<br>
                            <strong>Karakteristik:</strong> Penyusutan lebih besar di tahun awal<br>
                            <strong>Contoh:</strong> Aset Rp10.000.000, Umur 5 tahun<br>
                            → Tahun 1: 10.000.000 × (2/5) = <strong>Rp4.000.000</strong><br>
                            → Tahun 2: 6.000.000 × (2/5) = <strong>Rp2.400.000</strong><br>
                            → Dan seterusnya...
                        </p>
                    </div>
                    
                    <div>
                        <h4 style="color: #333; margin-bottom: 10px;">3. Jumlah Angka Tahun (Sum of Years Digits)</h4>
                        <p style="line-height: 1.8;">
                            <strong>Formula:</strong> (Sisa Umur / Jumlah Angka Tahun) × (Cost - Salvage)<br>
                            <strong>Karakteristik:</strong> Penyusutan menurun secara bertahap<br>
                            <strong>Contoh:</strong> Aset Rp10.000.000, Residu Rp1.000.000, Umur 5 tahun<br>
                            → Jumlah angka tahun = 5+4+3+2+1 = 15<br>
                            → Tahun 1: (5/15) × 9.000.000 = <strong>Rp3.000.000</strong><br>
                            → Tahun 2: (4/15) × 9.000.000 = <strong>Rp2.400.000</strong><br>
                            → Dan seterusnya...
                        </p>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Modal Hitung Penyusutan -->
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
                        <label>Periode Tahun Ke-</label>
                        <input type="number" name="period_year" id="calc_period_year" min="1" required>
                    </div>
                    <button type="submit" class="btn-sm btn-primary btn-block">🔢 Hitung Penyusutan</button>
                </form>
            </div>
        </div>
        
        <!-- Modal Catat Penyusutan -->
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
                        <label>Periode Tahun Ke-</label>
                        <input type="number" name="period_year" id="record_period_year" min="1" required>
                    </div>
                    <div class="form-group">
                        <label>Tanggal Pencatatan</label>
                        <input type="date" name="period_date" required value="{datetime.now().strftime('%Y-%m-%d')}">
                    </div>
                    <button type="submit" class="btn-sm btn-success btn-block">💾 Catat Jurnal</button>
                </form>
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
            }});
        }});
        
        function showDepreciationModal(assetId, assetName, periodYear) {{
            document.getElementById('calc_asset_id').value = assetId;
            document.getElementById('calc_asset_name').value = assetName;
            document.getElementById('calc_period_year').value = periodYear;
            document.getElementById('depreciationModal').style.display = 'block';
        }}
        
        function showRecordModal(assetId, assetName, periodYear) {{
            document.getElementById('record_asset_id').value = assetId;
            document.getElementById('record_asset_name').value = assetName;
            document.getElementById('record_period_year').value = periodYear;
            document.getElementById('recordModal').style.display = 'block';
        }}
        
        function closeModal(modalId) {{
            document.getElementById(modalId).style.display = 'none';
        }}
        
        // Close modal ketika klik di luar
        window.onclick = function(event) {{
            if (event.target.className === 'modal') {{
                event.target.style.display = 'none';
            }}
        }}
        </script>
    </body>
    </html>
    """
    
    return html
#==============Dashboard===============
def generate_akuntan_dashboard():
    """Generate dashboard akuntan dengan menu lengkap"""
    username = session.get('username', 'User')
    
    accounts = get_all_accounts()
    journals = get_journal_entries()
    
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
                    <li><a href="/dashboard/akuntan" class="active"><span class="icon">🏠</span> Dashboard</a></li>
                    <li><a href="/akuntan/accounts"><span class="icon">📋</span> Daftar Akun</a></li>
                    <li><a href="/akuntan/journal-crj"><span class="icon">💵</span> Jurnal CRJ</a></li>
                    <li><a href="/akuntan/journal-cpj"><span class="icon">💸</span> Jurnal CPJ</a></li>
                    <li><a href="/akuntan/journal-gj"><span class="icon">📝</span> Jurnal Umum</a></li>
                    <li><a href="/akuntan/adjustment-journal"><span class="icon">🔧</span> Jurnal Penyesuaian</a></li>
                    <li><a href="/akuntan/closing-journal"><span class="icon">🔒</span> Jurnal Penutup</a></li>
                    <li><a href="/akuntan/reversing-journal"><span class="icon">🔄</span> Jurnal Pembalik</a></li>
                    <li><a href="/akuntan/inventory"><span class="icon">📦</span> Inventory</a></li>
                    <li><a href="/akuntan/assets"><span class="icon">🏢</span> Aset & Penyusutan</a></li>
                    <li><a href="/akuntan/ledger"><span class="icon">📚</span> Buku Besar</a></li>
                    <li><a href="/akuntan/trial-balance"><span class="icon">⚖️</span> Neraca Saldo</a></li>
                    <li><a href="/akuntan/financial-statements"><span class="icon">📊</span> Laporan Keuangan</a></li>
                    <li><a href="/logout"><span class="icon">🚪</span> Logout</a></li>
                </ul>
            </div>
            
            <div class="main-content">
                <div class="top-bar">
                    <h1>Dashboard Akuntan</h1>
                    <div class="date-time" id="datetime"></div>
                </div>
                
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">📋</div>
                        <div class="stat-value">{len(accounts)}</div>
                        <div class="stat-label">Total Akun</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📝</div>
                        <div class="stat-value">{len(journals)}</div>
                        <div class="stat-label">Total Jurnal Entry</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">💵</div>
                        <div class="stat-value">{format_rupiah(sum(j.get('debit', 0) for j in journals))}</div>
                        <div class="stat-label">Total Debit</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">💸</div>
                        <div class="stat-value">{format_rupiah(sum(j.get('credit', 0) for j in journals))}</div>
                        <div class="stat-label">Total Kredit</div>
                    </div>
                </div>
                
                <div class="content-section">
                    <h2>📊 Siklus Akuntansi</h2>
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px;">
                        <a href="/akuntan/accounts" class="btn-sm btn-primary btn-block">1️⃣ Daftar Akun</a>
                        <a href="/akuntan/journal-crj" class="btn-sm btn-success btn-block">2️⃣ Jurnal Khusus</a>
                        <a href="/akuntan/ledger" class="btn-sm btn-info btn-block">3️⃣ Buku Besar</a>                        <a href="/akuntan/trial-balance" class="btn-sm btn-warning btn-block">4️⃣ Neraca Saldo</a>
                        <a href="/akuntan/adjustment-journal" class="btn-sm btn-primary btn-block">5️⃣ Penyesuaian</a>
                        <a href="/akuntan/financial-statements" class="btn-sm btn-success btn-block">6️⃣ Laporan Keuangan</a>
                        <a href="/akuntan/closing-journal" class="btn-sm btn-danger btn-block">7️⃣ Penutupan</a>
                        <a href="/akuntan/reversing-journal" class="btn-sm btn-info btn-block">8️⃣ Pembalikan</a>
                    </div>
                </div>
                
                <div class="content-section">
                    <h2>🔧 Fitur Tambahan</h2>
                    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px;">
                        <a href="/akuntan/inventory" class="btn-sm btn-primary btn-block">📦 Inventory</a>
                        <a href="/akuntan/assets" class="btn-sm btn-warning btn-block">🏢 Aset & Penyusutan</a>
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

# ============== ROUTES - DASHBOARDS ==============

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