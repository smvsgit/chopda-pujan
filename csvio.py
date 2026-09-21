"""CSV import and export for Centers, Members, Sankalps and Collections.

Design notes
------------
* Every export can be fed straight back in as an import. Round-tripping is the
  whole point, so the column names on both sides are identical.
* Rows are matched on a stable business key, never on the database id:
  centers by English name, members by Member ID, sankalps by
  (Member ID + year + type + firm/company + person), collections by
  (Member ID + year + sankalp name + date + amount). That means a file edited
  in Excel still lines up.
* Import runs in two passes. `validate` reports every problem without writing
  anything; `commit` writes only if the whole file is clean. A half-imported
  file is worse than no import.
"""

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

MAX_ROWS = 20000
MAX_BYTES = 8 * 1024 * 1024


def _s(row, key, default=''):
    v = row.get(key)
    return default if v is None else str(v).strip()


def _decimal(value, field):
    try:
        d = Decimal(str(value).replace(',', '').strip())
    except (InvalidOperation, AttributeError, ValueError):
        raise ValueError(f'{field}: "{value}" is not a number')
    if d < 0:
        raise ValueError(f'{field} cannot be negative')
    return d


def _date(value, field):
    v = str(value).strip()
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f'{field}: "{value}" is not a date (use YYYY-MM-DD)')


def _year(value, field):
    try:
        y = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f'{field}: "{value}" is not a year')
    if not 2000 <= y <= 2100:
        raise ValueError(f'{field}: {y} is out of range')
    return y


def write_csv(headers, rows):
    """Return CSV text with a UTF-8 BOM so Excel renders Gujarati correctly."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=headers, extrasaction='ignore',
                       lineterminator='\r\n')
    w.writeheader()
    for r in rows:
        w.writerow({h: ('' if r.get(h) is None else r.get(h)) for h in headers})
    return '\ufeff' + buf.getvalue()


def read_csv(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError('File is larger than 8 MB')
    for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('Could not read the file. Save it as CSV UTF-8.')
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError('The file has no header row')
    reader.fieldnames = [(f or '').strip() for f in reader.fieldnames]
    rows = list(reader)
    if len(rows) > MAX_ROWS:
        raise ValueError(f'File has more than {MAX_ROWS} rows')
    return reader.fieldnames, rows


# ============================ SPECS ============================
# Each spec: the headers, and the row builders / importers wired up in app.py.

CENTER_HEADERS = ['name_en', 'name_gu', 'city', 'phone', 'email', 'is_active']

MEMBER_HEADERS = ['member_code', 'name', 'member_type', 'center', 'mobile',
                  'email', 'dharmada_type', 'dharmada_amount', 'address',
                  'firms', 'companies']

# designation and pan_number are gone: designation follows the entity type and
# per-person PAN is not collected. person_code is the partner's Member ID.
PARTNER_HEADERS = ['member_code', 'entity_type', 'entity_name', 'entity_email',
                   'entity_contact', 'entity_address', 'entity_pan',
                   'person_code', 'person_name', 'mobile', 'email',
                   'person_center']

SANKALP_HEADERS = ['member_code', 'member_name', 'pujan_year', 'sankalp_type',
                   'firm_or_company', 'partner_or_director', 'sankalp_name',
                   'amount', 'primary_center', 'collecting_center',
                   'collected', 'pending', 'carried_from_year']

COLLECTION_HEADERS = ['member_code', 'pujan_year', 'sankalp_name',
                      'collection_date', 'amount', 'center', 'remarks']

USER_HEADERS = ['username', 'full_name', 'role', 'center', 'mobile', 'email',
                'language', 'is_active', 'password']

SPECS = {
    'centers':     {'headers': CENTER_HEADERS,     'label': 'Centers'},
    'members':     {'headers': MEMBER_HEADERS,     'label': 'Members'},
    'partners':    {'headers': PARTNER_HEADERS,    'label': 'Firms, Companies & Partners'},
    'sankalps':    {'headers': SANKALP_HEADERS,    'label': 'Sankalps'},
    'collections': {'headers': COLLECTION_HEADERS, 'label': 'Collections'},
    'users':       {'headers': USER_HEADERS,       'label': 'Users'},
}


def detect_kind(headers):
    """Work out which kind of file this is from its header row.

    Picking the wrong file in the wrong dialog used to fail with a confusing
    "Missing required column(s): name". Now the file identifies itself.
    """
    got = {(h or '').strip().lower() for h in headers}
    best, best_score = None, 0
    for kind, spec in SPECS.items():
        want = {h.lower() for h in spec['headers']}
        overlap = len(got & want)
        score = overlap / max(len(want), 1)
        # A header set has to look mostly right before we accept it.
        if overlap >= 2 and score > best_score:
            best, best_score = kind, score
    return best if best_score >= 0.6 else None