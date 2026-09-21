import calendar
import io
import random
import json
import threading
import time
import os
import re
import uuid
import bcrypt
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import quote_plus
from flask import (Flask, request, session, redirect, url_for, abort, send_file,
                   render_template, jsonify)
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy.exc import IntegrityError
from flask import Response
from flask_wtf.csrf import CSRFProtect, generate_csrf
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
import segno

import csvio
import notify

# ---- India Standard Time: every timestamp is stored UTC-aware and
#      rendered in IST, because the whole system is used in Gujarat. ----
IST = timezone(timedelta(hours=5, minutes=30))

def utcnow():
    return datetime.now(timezone.utc)

def to_ist(dt, fmt='%d-%m-%Y %H:%M'):
    if not dt:
        return None
    if dt.tzinfo is None:                    # rows written before this change
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime(fmt)


app = Flask(__name__, static_folder='static', template_folder='templates')

# The interface language a new account starts in, and the language used
# whenever a stored choice is missing or unreadable. English by default; set
# DEFAULT_LANGUAGE=gu in the environment to start everyone in Gujarati.
# Anyone can still switch their own copy with the language button.
DEFAULT_LANGUAGE = os.environ.get('DEFAULT_LANGUAGE', 'en')
if DEFAULT_LANGUAGE not in ('en', 'gu'):
    DEFAULT_LANGUAGE = 'en'

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if os.environ.get('FLASK_DEBUG') == '1':
        SECRET_KEY = 'dev-only-insecure-key'
    else:
        raise RuntimeError('SECRET_KEY environment variable is required.')
app.config['SECRET_KEY'] = SECRET_KEY

DATABASE_URL = (os.environ.get('DATABASE_URL') or '').strip()
if not DATABASE_URL:
    if os.environ.get('FLASK_DEBUG') == '1':
        DATABASE_URL = 'sqlite:////tmp/smvs_chopdapujan_dev.sqlite3'
    else:
        raise RuntimeError('DATABASE_URL environment variable is required.')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Seed credentials are required in production. Development keeps the historic
# defaults only when FLASK_DEBUG=1; production must fail closed instead of
# creating known-password accounts if Coolify secrets are missing.
if os.environ.get('FLASK_DEBUG') != '1':
    for _required_secret in ('ADMIN_PASSWORD', 'SANT_PASSWORD'):
        if not os.environ.get(_required_secret):
            raise RuntimeError(f'{_required_secret} environment variable is required.')
# Small pool PER GUNICORN WORKER. 4 workers x (5+5) = 40 < Postgres default 100.
# pool_pre_ping avoids "server closed the connection unexpectedly" after idle.
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite'):
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}
else:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_size': 5, 'max_overflow': 5, 'pool_pre_ping': True, 'pool_recycle': 1800,
    }

# In development (FLASK_DEBUG=1 via docker-compose.override.yml) templates are
# re-read on every request and static files are not cached, so editing app.html
# or style.css needs nothing more than a browser refresh.
if os.environ.get('FLASK_DEBUG') == '1':
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
    app.jinja_env.auto_reload = True

app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('COOKIE_SECURE', '0') == '1'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
# Uploads are CSV files and one poster image. A cap stops a large body from
# being used to exhaust memory.
app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('MAX_UPLOAD_MB', '12')) * 1024 * 1024

# Mounted under a path on a shared domain (smvs.org/chopdapujan), the cookie
# must be scoped to that path and named distinctly, or it will collide with
# whatever else lives on the domain.
_ROOT = os.environ.get('APP_ROOT', '').rstrip('/')
if _ROOT:
    app.config['APPLICATION_ROOT'] = _ROOT
    app.config['SESSION_COOKIE_PATH'] = _ROOT
app.config['SESSION_COOKIE_NAME'] = os.environ.get('COOKIE_NAME', 'chopdapujan_session')

# Behind nginx, the real scheme and host arrive in X-Forwarded-*. Without this
# Flask builds http:// URLs and marks secure cookies as insecure.
if os.environ.get('BEHIND_PROXY', '0') == '1':
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# User-generated files are persisted on the SMVS media bind, not in the
# container layer and not as new database BLOBs. In production MEDIA_ROOT is
# /app/media, bind-mounted from /srv/media/projects/smvs-chopdapujan/media.
MEDIA_ROOT = os.path.abspath(os.environ.get('MEDIA_ROOT', '/app/media'))


def _media_path(relative_path):
    """Return an absolute path below MEDIA_ROOT, rejecting path traversal."""
    relative_path = (relative_path or '').replace('\\', '/').lstrip('/')
    candidate = os.path.abspath(os.path.join(MEDIA_ROOT, relative_path))
    if os.path.commonpath([MEDIA_ROOT, candidate]) != MEDIA_ROOT:
        raise ValueError('Invalid media path')
    return candidate


def read_media_file(relative_path):
    if not relative_path:
        return None
    try:
        with open(_media_path(relative_path), 'rb') as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def write_media_file(category, original_name, data):
    """Atomically write bytes below MEDIA_ROOT and return the relative path."""
    clean_category = '/'.join(
        part for part in (category or '').replace('\\', '/').split('/')
        if part and part not in ('.', '..')
    )
    if not clean_category:
        raise ValueError('Media category is required')
    ext = os.path.splitext(original_name or '')[1].lower()
    if not re.fullmatch(r'\.[a-z0-9]{1,10}', ext or ''):
        ext = ''
    rel = f"{clean_category}/{uuid.uuid4().hex}{ext}"
    path = _media_path(rel)
    os.makedirs(os.path.dirname(path), mode=0o750, exist_ok=True)
    tmp = path + '.tmp-' + uuid.uuid4().hex
    try:
        with open(tmp, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return rel


def delete_media_file(relative_path):
    if not relative_path:
        return
    try:
        os.unlink(_media_path(relative_path))
    except FileNotFoundError:
        pass


db = SQLAlchemy(app)

# ---------------------------------------------------------------------------
# Schema changes
#
# Alembic, through Flask-Migrate. The hand-written "ALTER TABLE ... IF NOT
# EXISTS" list below is still run, and still has to be, for one release: an
# existing database has no alembic_version row, so Alembic does not know which
# migrations it already has. `flask db stamp head` fixes that - see MIGRATIONS.md
# - and until every deployment has been stamped, the idempotent ALTERs are what
# keeps an un-stamped database working.
#
# From the next schema change onward: add the column to the model, run
# `flask db migrate -m "..."`, read the generated file, and commit it. Do not
# add another line to the ALTER list.
# ---------------------------------------------------------------------------
migrate = Migrate(app, db, compare_type=True)
csrf = CSRFProtect(app)


@app.after_request
def security_headers(response):
    """Headers that limit what a hijacked page or an injected script can do."""
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault('Permissions-Policy',
                                'geolocation=(), microphone=(), camera=(self)')
    # The page pulls fonts and icons from two CDNs and reads the camera for QR
    # scanning; everything else is same-origin. 'unsafe-inline' is needed
    # because the interface is one inline script and inline styles - worth
    # removing if the front end is ever split into files.
    response.headers.setdefault('Content-Security-Policy', "; ".join([
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://fonts.googleapis.com",
        "font-src 'self' data: https://cdnjs.cloudflare.com https://fonts.gstatic.com",
        "img-src 'self' data: blob:",
        "connect-src 'self'",
        "media-src 'self' blob:",
        "frame-ancestors 'self'",
        "base-uri 'self'",
        "form-action 'self'",
    ]))
    if app.config['SESSION_COOKIE_SECURE']:
        response.headers.setdefault('Strict-Transport-Security',
                                    'max-age=31536000; includeSubDomains')
    return response


@app.after_request
def set_csrf_cookie(response):
    """Readable-by-JS cookie holding the CSRF token; the SPA echoes it back
    in the X-CSRFToken header on every write request."""
    response.set_cookie('csrf_token', generate_csrf(), samesite='Lax',
                        secure=app.config['SESSION_COOKIE_SECURE'])
    return response


# ======================== MODELS ========================

class Role(db.Model):
    """A named set of permissions.

    Roles are rows, not code, so an admin can add "Center Accountant" or
    "HO Coordinator" without a release. The four built-in roles are seeded as
    system roles and cannot be deleted, though their permissions can be edited.

    `center_scoped` is the flag that matters: a centre-scoped role only ever
    sees its own centre's data, whatever its permissions say. Head-office roles
    are not scoped and see everything they hold permission for.
    """
    __tablename__ = 'roles'
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), nullable=False)
    name_gu = db.Column(db.String(100), default='')
    description = db.Column(db.String(300), default='')
    center_scoped = db.Column(db.Boolean, default=True, nullable=False)
    is_system = db.Column(db.Boolean, default=False, nullable=False)
    permissions = db.Column(db.Text)              # {"key": true, ...}
    sort_order = db.Column(db.Integer, default=100)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    @property
    def perm_map(self):
        try:
            saved = json.loads(self.permissions) if self.permissions else {}
        except (ValueError, TypeError):
            saved = {}
        return {k: bool(saved.get(k, False)) for k in ALL_PERMISSIONS}

    def to_dict(self, with_counts=False):
        d = {
            'id': self.id, 'key': self.key, 'name': self.name,
            'name_gu': self.name_gu or '', 'description': self.description or '',
            'center_scoped': bool(self.center_scoped),
            'is_system': bool(self.is_system),
            'is_active': bool(self.is_active),
            'sort_order': self.sort_order or 100,
            'permissions': self.perm_map,
            'granted': sum(1 for v in self.perm_map.values() if v),
            'total': len(ALL_PERMISSIONS),
        }
        if with_counts:
            d['users'] = User.query.filter_by(role=self.key).count()
        return d


class Center(db.Model):
    __tablename__ = 'centers'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    name_en = db.Column(db.String(200), default='')
    address = db.Column(db.Text)
    city = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    email = db.Column(db.String(200), default='')   # centre notification inbox
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    users = db.relationship('User', backref='center_ref', lazy='dynamic', overlaps='user_center')
    members = db.relationship('Member', backref='center_ref', lazy='dynamic', overlaps='center,members')
    # Sankalp now has TWO foreign keys to centers (center_id = who collects,
    # owner_center_id = the primary), so the join has to be spelled out.
    sankalps = db.relationship('Sankalp', backref='center_ref', lazy='dynamic',
                               foreign_keys='Sankalp.center_id', overlaps='center,sankalps')
    # Centers are never hard-deleted (see api_delete_center) - they are
    # deactivated, so no cascade is declared here on purpose.

    @property
    def label(self):
        """Name in the language the signed-in user picked. The list used to
        always show the Gujarati column even when the UI was in English."""
        if session.get('lang', 'en') == 'gu':
            return self.name or self.name_en or ''
        return self.name_en or self.name or ''

    def to_dict(self, counts=None):
        return {
            'id': self.id, 'name': self.name, 'name_en': self.name_en or self.name,
            'label': self.label,
            'address': self.address, 'city': self.city, 'phone': self.phone,
            'email': self.email or '',
            'is_active': self.is_active,
            'created_at': to_ist(self.created_at),
            # Counted in one grouped query by the list endpoint and handed in
            # here. Falls back to a per-row COUNT only when nothing was
            # supplied - a single centre fetched on its own, where one extra
            # query costs less than a GROUP BY over the whole table.
            #
            # Written as an if, not dict.get(key, default): the default of
            # .get() is evaluated whether it is needed or not, so the COUNTs
            # fired on every row even when the numbers had been passed in.
            'members_count': (counts['members'] if counts and 'members' in counts
                              else self.members.filter_by(is_active=True).count()),
            'sankalps_count': (counts['sankalps'] if counts and 'sankalps' in counts
                               else self.sankalps.count()),
        }


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    # Set on a seeded or admin-created account, cleared the moment the person
    # chooses their own password. Until then nothing else in the app opens.
    must_change_password = db.Column(db.Boolean, default=False, nullable=False)
    password_changed_at = db.Column(db.DateTime)
    full_name = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(50), nullable=False)
    center_id = db.Column(db.Integer, db.ForeignKey('centers.id'), nullable=True)
    mobile = db.Column(db.String(20))
    email = db.Column(db.String(200))
    language = db.Column(db.String(5), default=lambda: DEFAULT_LANGUAGE)
    # Per-user overrides on top of the role defaults, as {"key": true/false}.
    permissions = db.Column(db.Text)
    # 'team_member' = works across every centre from Head Office.
    # 'center'      = belongs to one centre and only ever sees that centre.
    # Left blank on older rows, where the role's own scope still decides.
    user_type = db.Column(db.String(20))
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    center = db.relationship('Center', overlaps='center_ref,users')

    def to_dict(self):
        return {
            'id': self.id, 'username': self.username, 'full_name': self.full_name,
            'role': self.role, 'center_id': self.center_id,
            'center_name': self.center.label if self.center else 'HO',
            'permissions': effective_permissions(self),
            'role_name': (get_role(self.role).name if get_role(self.role) else self.role),
            'center_scoped': is_center_scoped(self),
            'user_type': self.user_type or default_user_type(self.role),
            # What the Users list and the sidebar show in one field.
            'place': ('Team Member' if not is_center_scoped(self)
                      else (self.center.label if self.center else 'HO')),
            'mobile': self.mobile, 'email': self.email,
            'language': self.language or DEFAULT_LANGUAGE, 'is_active': self.is_active,
            'must_change_password': bool(self.must_change_password),
            'created_at': to_ist(self.created_at)
        }

    def set_password(self, pwd):
        self.password_hash = bcrypt.hashpw(pwd.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    def check_password(self, pwd):
        return bcrypt.checkpw(pwd.encode('utf-8'), self.password_hash.encode('utf-8'))


class Member(db.Model):
    __tablename__ = 'members'
    id = db.Column(db.Integer, primary_key=True)
    center_id = db.Column(db.Integer, db.ForeignKey('centers.id'), nullable=False)
    # Human-readable ID printed on passes and used as the key for CSV import.
    member_code = db.Column(db.String(50), unique=True, index=True)
    member_type = db.Column(db.String(50), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    mobile = db.Column(db.String(20))
    email = db.Column(db.String(200))
    address = db.Column(db.Text)
    firm_name = db.Column(db.String(200))
    firm_address = db.Column(db.Text)
    firm_contact = db.Column(db.String(20))
    company_name = db.Column(db.String(200))
    company_address = db.Column(db.Text)
    company_contact = db.Column(db.String(20))
    designation = db.Column(db.String(100))
    pan_number = db.Column(db.String(20))              # kept for old data
    # Dharmada: the kind of pledge (R / I/R / P / N) and its amount. Held apart
    # from the sankalp figures because it is accounted for separately.
    # Set to the pujan year when this member was registered at the door on the
    # day itself. That - not the year the record happens to have been created -
    # is what "New" means on the attendance grid: a walk-in, as opposed to
    # somebody who was on the list beforehand.
    event_registered_year = db.Column(db.Integer)
    dharmada_type = db.Column(db.String(10), default='')
    dharmada_amount = db.Column(db.Numeric(12, 2), default=0)
    is_active = db.Column(db.Boolean, default=True)
    # The centre sant decides who travels to Head Office for the pujan. Once
    # ticked the member belongs to HO's list for that year and their centre
    # stops entering sankalp against them.
    attend_at_ho = db.Column(db.Boolean, default=False, nullable=False)
    ho_year = db.Column(db.Integer)
    ho_marked_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    ho_marked_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow)
    center = db.relationship('Center', overlaps='center_ref,members')
    ho_marker = db.relationship('User', foreign_keys=[ho_marked_by])
    sankalps = db.relationship('Sankalp', backref='member_ref', lazy='dynamic',
                               overlaps='member,sankalps', cascade='all, delete-orphan')
    # A member may hold any number of firms and companies. The old single
    # firm_name / company_name columns are kept only so existing rows migrate.
    entities = db.relationship('Entity', backref='member', lazy='select',
                               cascade='all, delete-orphan', order_by='Entity.id')

    @property
    def firms(self):
        return [e for e in self.entities if e.entity_type == 'firm']

    @property
    def companies(self):
        return [e for e in self.entities if e.entity_type == 'company']

    @property
    def display_name(self):
        primary = None
        if self.member_type == 'firm':
            primary = self.firms[0] if self.firms else None
        elif self.member_type == 'company':
            primary = self.companies[0] if self.companies else None
        if primary:
            extra = len(self.firms) + len(self.companies) - 1
            suffix = f" +{extra}" if extra > 0 else ''
            return f"{self.name} ({primary.name}{suffix})"
        return self.name

    def to_dict(self, include_sankalp=False, year=None):
        d = {
            'id': self.id, 'member_code': self.member_code or '',
            'center_id': self.center_id,
            'center_name': self.center.label if self.center else '',
            'member_type': self.member_type, 'name': self.name,
            'display_name': self.display_name,
            'mobile': self.mobile, 'email': self.email,
            'address': self.address,
            'dharmada_type': self.dharmada_type or '',
            'dharmada_amount': float(self.dharmada_amount or 0),
            'event_registered_year': self.event_registered_year,
            'firm_name': self.firm_name, 'firm_address': self.firm_address,
            'firm_contact': self.firm_contact,
            'company_name': self.company_name, 'company_address': self.company_address,
            'company_contact': self.company_contact,
            'designation': self.designation, 'pan_number': self.pan_number,
            'is_active': self.is_active,
            'attend_at_ho': bool(self.attend_at_ho),
            'ho_year': self.ho_year,
            'ho_marked_by': self.ho_marker.full_name if self.ho_marker else '',
            'ho_marked_at': to_ist(self.ho_marked_at),
            'entities': [e.to_dict() for e in self.entities],
            'firms_count': len(self.firms), 'companies_count': len(self.companies),
            'created_at': to_ist(self.created_at)
        }
        if include_sankalp and year:
            # A member can hold several pledges in one year - one per firm, one
            # per company, one per partner. This used to call .first() and show
            # only one of them, so the Members list under-reported the totals.
            sks = Sankalp.query.filter_by(member_id=self.id, pujan_year=year).all()
            if sks:
                rows = []
                for s_ in sks:
                    got = sum(float(c.amount) for c in s_.collections)
                    rows.append({
                        'id': s_.id, 'amount': float(s_.amount), 'collected': got,
                        'pending': float(s_.amount) - got,
                        'sankalp_type': s_.sankalp_type, 'sankalp_name': s_.sankalp_name,
                        'entity_name': s_.entity.name if s_.entity else '',
                        'person_name': s_.person.name if s_.person else '',
                        'center_name': s_.center.label if s_.center else '',
                    })
                d['sankalps'] = rows
                d['sankalp'] = {                       # roll-up for the list row
                    'count': len(rows),
                    'amount': sum(r['amount'] for r in rows),
                    'collected': sum(r['collected'] for r in rows),
                    'pending': sum(r['pending'] for r in rows),
                }
            else:
                d['sankalps'] = []
                prev_years = Sankalp.query.filter_by(member_id=self.id).order_by(Sankalp.pujan_year.desc()).all()
                d['previous_sankalps'] = [{
                    'year': s.pujan_year, 'amount': float(s.amount),
                    'collected': sum(float(c.amount) for c in s.collections),
                    'pending': float(s.amount) - sum(float(c.amount) for c in s.collections),
                    'sankalp_type': s.sankalp_type, 'sankalp_name': s.sankalp_name
                } for s in prev_years[:3]] if prev_years else []
        return d


class Entity(db.Model):
    """A firm or a company belonging to a member."""
    __tablename__ = 'entities'
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    entity_type = db.Column(db.String(20), nullable=False)      # 'firm' | 'company'
    name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.Text, default='')
    email = db.Column(db.String(200), default='')
    contact = db.Column(db.String(20), default='')
    pan_number = db.Column(db.String(20), default='')
    gst_number = db.Column(db.String(30), default='')
    created_at = db.Column(db.DateTime, default=utcnow)
    people = db.relationship('EntityPerson', backref='entity', lazy='select',
                             cascade='all, delete-orphan', order_by='EntityPerson.id')

    __table_args__ = (db.Index('ix_entity_member_type', 'member_id', 'entity_type'),)

    @property
    def role_label(self):
        return 'Director' if self.entity_type == 'company' else 'Partner'

    def to_dict(self):
        return {
            'id': self.id, 'member_id': self.member_id,
            'entity_type': self.entity_type, 'name': self.name,
            'address': self.address or '', 'email': self.email or '',
            'contact': self.contact or '', 'pan_number': self.pan_number or '',
            'gst_number': self.gst_number or '',
            'people': [p.to_dict() for p in self.people],
        }


class EntityPerson(db.Model):
    """A partner of a firm, or a director of a company."""
    __tablename__ = 'entity_persons'
    id = db.Column(db.Integer, primary_key=True)
    entity_id = db.Column(db.Integer, db.ForeignKey('entities.id'), nullable=False)
    # Every partner/director carries their own Member ID. Designation is no
    # longer typed in - it is Partner for a firm and Director for a company,
    # which the entity already tells us. PAN is not collected per person.
    member_code = db.Column(db.String(50), index=True)
    name = db.Column(db.String(200), nullable=False)
    designation = db.Column(db.String(100), default='')
    mobile = db.Column(db.String(20), default='')
    email = db.Column(db.String(200), default='')
    pan_number = db.Column(db.String(20), default='')
    # A partner does not have to sit in the same centre as the firm. When a
    # pledge is taken in this person's name, THIS centre collects it.
    center_id = db.Column(db.Integer, db.ForeignKey('centers.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    center = db.relationship('Center', overlaps='center_ref')

    @property
    def role_label(self):
        ent = self.entity
        return 'Director' if (ent and ent.entity_type == 'company') else 'Partner'

    @property
    def effective_center_id(self):
        """Falls back to the firm's owning member's centre."""
        if self.center_id:
            return self.center_id
        ent = self.entity
        return ent.member.center_id if ent and ent.member else None

    def to_dict(self):
        return {
            'id': self.id, 'entity_id': self.entity_id, 'name': self.name,
            'member_code': self.member_code or '',
            'designation': self.designation or self.role_label,
            'mobile': self.mobile or '',
            'email': self.email or '', 'pan_number': self.pan_number or '',
            'center_id': self.center_id,
            'center_name': self.center.label if self.center else '',
            'effective_center_id': self.effective_center_id,
        }


class Sankalp(db.Model):
    __tablename__ = 'sankalps'
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    center_id = db.Column(db.Integer, db.ForeignKey('centers.id'), nullable=False)
    pujan_year = db.Column(db.Integer, nullable=False)
    sankalp_type = db.Column(db.String(50), nullable=False)
    sankalp_name = db.Column(db.String(300), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    # Exactly which firm/company and which partner/director this pledge is for.
    # A member can hold several of each, so it can no longer be inferred.
    entity_id = db.Column(db.Integer, db.ForeignKey('entities.id'), nullable=True)
    person_id = db.Column(db.Integer, db.ForeignKey('entity_persons.id'), nullable=True)
    # center_id above = who COLLECTS (the partner's centre when the pledge is in
    # a partner's name). owner_center_id = the PRIMARY, the member's own centre.
    # Carry-forward follows the primary so the pledge lineage survives a partner
    # moving centre.
    owner_center_id = db.Column(db.Integer, db.ForeignKey('centers.id'), nullable=True)
    # Set when this pledge was rolled over from last year's.
    carried_from_id = db.Column(db.Integer, db.ForeignKey('sankalps.id'), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=utcnow)
    # Stamped when the amount is confirmed in bulk edit, so reopening the grid
    # shows which rows have already been done.
    verified_at = db.Column(db.DateTime)
    verified_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    member = db.relationship('Member', overlaps='member_ref,sankalps')
    center = db.relationship('Center', foreign_keys=[center_id], overlaps='center_ref,sankalps')
    owner_center = db.relationship('Center', foreign_keys=[owner_center_id], overlaps='center_ref,sankalps')
    entity = db.relationship('Entity', foreign_keys=[entity_id])
    carried_from = db.relationship('Sankalp', remote_side=[id], foreign_keys=[carried_from_id])
    person = db.relationship('EntityPerson', foreign_keys=[person_id])
    collections = db.relationship('Collection', backref='sankalp_ref', lazy='dynamic',
                                  cascade='all, delete-orphan')
    changes = db.relationship('SankalpChange', backref='sankalp', lazy='dynamic',
                              cascade='all, delete-orphan')
    passes = db.relationship('Pass', backref='sankalp_link', lazy='dynamic',
                             overlaps='sankalp', cascade='all, delete-orphan')

    def to_dict(self, with_collection=False):
        d = {
            'id': self.id, 'member_id': self.member_id,
            'member_name': self.member.display_name if self.member else '',
            'member_code': (self.member.member_code or '') if self.member else '',
            'member_mobile': self.member.mobile if self.member else '',
            'center_id': self.center_id,
            'center_name': self.center.label if self.center else '',
            'pujan_year': self.pujan_year, 'sankalp_type': self.sankalp_type,
            'sankalp_name': self.sankalp_name,
            'amount': float(self.amount),
            'entity_id': self.entity_id,
            'entity_name': self.entity.name if self.entity else '',
            'entity_type': self.entity.entity_type if self.entity else '',
            'person_id': self.person_id,
            'person_name': self.person.name if self.person else '',
            'person_designation': (self.person.designation or '') if self.person else '',
            # The firm shown as secondary context in the collecting centre.
            'owner_center_id': self.owner_center_id,
            'owner_center_name': self.owner_center.label if self.owner_center else '',
            'is_cross_center': bool(self.owner_center_id and self.owner_center_id != self.center_id),
            'carried_from_id': self.carried_from_id,
            'carried_from_year': self.carried_from.pujan_year if self.carried_from else None,
            'created_at': to_ist(self.created_at)
        }
        if with_collection:
            collected = sum(float(c.amount) for c in self.collections)
            d['collected'] = collected
            d['pending'] = float(self.amount) - collected
            d['changes'] = [ch.to_dict() for ch in
                            self.changes.order_by(SankalpChange.changed_at.desc())]
            d['collections_list'] = [{
                'id': c.id, 'amount': float(c.amount),
                'date': c.collection_date.strftime('%d-%m-%Y') if c.collection_date else '',
                'date_iso': c.collection_date.strftime('%Y-%m-%d') if c.collection_date else '',
                'remarks': c.remarks
            } for c in self.collections.order_by(Collection.collection_date.desc())]
        return d


class SankalpChange(db.Model):
    """Every revision of a pledge amount, with the reason. Centre staff may
    only change the amount, so this is the record of why the money moved."""
    __tablename__ = 'sankalp_changes'
    id = db.Column(db.Integer, primary_key=True)
    sankalp_id = db.Column(db.Integer, db.ForeignKey('sankalps.id'), nullable=False)
    old_amount = db.Column(db.Numeric(12, 2), nullable=False)
    new_amount = db.Column(db.Numeric(12, 2), nullable=False)
    remark = db.Column(db.Text, nullable=False)
    changed_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    changed_at = db.Column(db.DateTime, default=utcnow)
    user = db.relationship('User', foreign_keys=[changed_by])

    def to_dict(self):
        return {
            'id': self.id, 'old_amount': float(self.old_amount),
            'new_amount': float(self.new_amount), 'remark': self.remark,
            'changed_by': self.user.full_name if self.user else '',
            'changed_at': to_ist(self.changed_at),
        }


class AuditLog(db.Model):
    """Who did what, when, and what changed.

    Old and new values are stored as JSON so an edit can be read back field by
    field. Written for logins, logouts, and every create/update/delete on the
    records that matter.
    """
    __tablename__ = 'audit_log'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    username = db.Column(db.String(80))          # kept even if the user is removed
    full_name = db.Column(db.String(200))
    role = db.Column(db.String(30))
    center_id = db.Column(db.Integer, db.ForeignKey('centers.id'))
    action = db.Column(db.String(20), nullable=False, index=True)   # login|logout|create|update|delete|...
    entity_type = db.Column(db.String(40), index=True)              # member|sankalp|collection|center|user|pass
    entity_id = db.Column(db.Integer)
    entity_label = db.Column(db.String(300))
    pujan_year = db.Column(db.Integer, index=True)
    old_values = db.Column(db.Text)
    new_values = db.Column(db.Text)
    ip = db.Column(db.String(60))
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    center = db.relationship('Center', foreign_keys=[center_id])

    def _json(self, raw):
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}

    def to_dict(self):
        old, new = self._json(self.old_values), self._json(self.new_values)
        # Only the fields that actually moved, so an edit reads at a glance.
        def _blank(v):
            return v is None or v == '' or v is False
        changed = [{'field': k, 'old': old.get(k), 'new': new.get(k)}
                   for k in sorted(set(old) | set(new))
                   if old.get(k) != new.get(k)]
        if self.action == 'create':
            changed = [c for c in changed if not _blank(c['new'])]
        elif self.action == 'delete':
            changed = [c for c in changed if not _blank(c['old'])]
        return {
            'id': self.id, 'username': self.username or '', 'full_name': self.full_name or '',
            'role': self.role or '', 'center_name': self.center.label if self.center else '',
            'action': self.action, 'entity_type': self.entity_type or '',
            'entity_id': self.entity_id, 'entity_label': self.entity_label or '',
            'pujan_year': self.pujan_year, 'ip': self.ip or '',
            'when': to_ist(self.created_at, '%d-%m-%Y %H:%M:%S'),
            'old_values': old, 'new_values': new, 'changes': changed,
        }


class FirmNumber(db.Model):
    """A number for each firm or company doing pujan this year.

    Kept in its own table because a firm exists across years but its number is
    per year, and because it should stay put once printed on a slip.
    """
    __tablename__ = 'firm_numbers'
    id = db.Column(db.Integer, primary_key=True)
    pujan_year = db.Column(db.Integer, nullable=False, index=True)
    entity_id = db.Column(db.Integer, db.ForeignKey('entities.id'), nullable=False)
    firm_no = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    __table_args__ = (
        db.UniqueConstraint('pujan_year', 'entity_id', name='uq_firmno_entity_year'),
        db.UniqueConstraint('pujan_year', 'firm_no', name='uq_firmno_year'),
    )


def firm_number_for(entity_id, year):
    """The firm's number for this year, creating it on first use."""
    if not entity_id:
        return None
    row = FirmNumber.query.filter_by(pujan_year=year, entity_id=entity_id).first()
    if row:
        return row.firm_no
    nxt = (db.session.query(db.func.max(FirmNumber.firm_no))
           .filter_by(pujan_year=year).scalar() or 0) + 1
    row = FirmNumber(pujan_year=year, entity_id=entity_id, firm_no=nxt)
    db.session.add(row)
    db.session.flush()
    return nxt


class Collection(db.Model):
    __tablename__ = 'collections'
    id = db.Column(db.Integer, primary_key=True)
    sankalp_id = db.Column(db.Integer, db.ForeignKey('sankalps.id'), nullable=False)
    center_id = db.Column(db.Integer, db.ForeignKey('centers.id'), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    collection_date = db.Column(db.Date, nullable=False)
    remarks = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=utcnow)
    center = db.relationship('Center', overlaps='center_ref,collections')

    def to_dict(self):
        return {
            'id': self.id, 'sankalp_id': self.sankalp_id,
            'center_id': self.center_id,
            'center_name': self.center.label if self.center else '',
            'amount': float(self.amount),
            'collection_date': self.collection_date.strftime('%d-%m-%Y') if self.collection_date else '',
            'collection_date_iso': self.collection_date.strftime('%Y-%m-%d') if self.collection_date else '',
            'remarks': self.remarks,
            'created_at': to_ist(self.created_at)
        }


class Pass(db.Model):
    __tablename__ = 'passes'
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    sankalp_id = db.Column(db.Integer, db.ForeignKey('sankalps.id'), nullable=True)
    center_id = db.Column(db.Integer, db.ForeignKey('centers.id'), nullable=False)
    pujan_year = db.Column(db.Integer, nullable=False)
    # Unique PER YEAR, not globally: YJ0001 must be reusable next Diwali.
    pass_number = db.Column(db.String(50), nullable=False)
    pass_type = db.Column(db.String(20), nullable=False)
    is_used = db.Column(db.Boolean, default=False)
    # So a second print can warn rather than quietly produce a duplicate.
    print_count = db.Column(db.Integer, default=0)
    last_printed_at = db.Column(db.DateTime)
    last_printed_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=utcnow)
    member = db.relationship('Member', overlaps='member_ref,passes')
    center = db.relationship('Center', overlaps='center_ref,passes')
    sankalp = db.relationship('Sankalp', overlaps='sankalp_ref,passes,sankalp_link')
    attendance_rows = db.relationship('Attendance', backref='pass_link', lazy='dynamic',
                                      overlaps='pas', cascade='all, delete-orphan')

    __table_args__ = (
        db.UniqueConstraint('pujan_year', 'pass_number', name='uq_pass_year_number'),
        db.Index('ix_pass_year_type', 'pujan_year', 'pass_type'),
    )

    def to_dict(self):
        return {
            'id': self.id, 'member_id': self.member_id,
            # The person's own name. Their firms are listed separately on the
            # pass, so display_name's "Name (Firm +2)" only repeated them.
            'member_name': self.member.name if self.member else '',
            'display_name': self.member.display_name if self.member else '',
            'member_code': (self.member.member_code or '') if self.member else '',
            'member_mobile': self.member.mobile if self.member else '',
            'sankalp_id': self.sankalp_id,
            'sankalp_amount': float(self.sankalp.amount) if self.sankalp else 0,
            'center_id': self.center_id,
            'center_name': self.center.label if self.center else '',
            'pujan_year': self.pujan_year,
            'pass_number': self.pass_number, 'pass_type': self.pass_type,
            'is_used': self.is_used,
            # Every firm/company this member pledged for in this year, so a
            # Yajman pass can print what it is actually for.
            'sankalp_lines': self._sankalp_lines(),
            'created_at': to_ist(self.created_at)
        }

    def _sankalp_lines(self):
        if not self.member:
            return []
        rows = Sankalp.query.filter_by(member_id=self.member_id,
                                       pujan_year=self.pujan_year).all()
        out = []
        for s_ in rows:
            got = sum(float(c.amount) for c in s_.collections)
            out.append({
                'entity_name': s_.entity.name if s_.entity else '',
                'entity_type': s_.entity.entity_type if s_.entity else '',
                'person_name': s_.person.name if s_.person else '',
                # Printed on the pass so the check-in desk can key the ID.
                'person_code': (s_.person.member_code or '') if s_.person else '',
                'sankalp_name': s_.sankalp_name,
                'amount': float(s_.amount), 'collected': got,
                'pending': float(s_.amount) - got,
            })
        if not out:
            # Registered at the door with no pledge yet: their firms still
            # belong on the pass, because that is how the holder recognises it
            # as theirs. Amounts stay empty rather than showing zero.
            for e in self.member.entities:
                out.append({
                    'entity_name': e.name, 'entity_type': e.entity_type,
                    'person_name': '', 'person_code': '',
                    'sankalp_name': e.name,
                    'amount': None, 'collected': None, 'pending': None,
                    'no_sankalp': True,
                })
        return out


class Attendance(db.Model):
    __tablename__ = 'attendance'
    id = db.Column(db.Integer, primary_key=True)
    pass_id = db.Column(db.Integer, db.ForeignKey('passes.id'), nullable=True)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    center_id = db.Column(db.Integer, db.ForeignKey('centers.id'), nullable=False)
    pujan_year = db.Column(db.Integer, nullable=False)
    check_in_time = db.Column(db.DateTime, default=utcnow)
    created_at = db.Column(db.DateTime, default=utcnow)
    # Handed out in order of arrival, starting at 1 each pujan year.
    seat_no = db.Column(db.Integer)
    slip_printed_at = db.Column(db.DateTime)
    slip_printed_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    # Guruji Darshan, in two stages, both on the same row as the check-in.
    # Darshan happens after arrival and belongs to the same person on the same
    # day, so the seat number both are read against is already here.
    #
    #   darshan_at      the attendance desk scanned them a second time with
    #                   Guruji Darshan ticked - they have gone to darshan.
    #                   One green tick after the seat number.
    #   darshan_done_at Guruji's desk pressed Save & Next - the pledge was
    #                   confirmed with them and darshan is finished.
    #                   Two green ticks after the seat number.
    #
    # Two columns rather than one status word: each stage is stamped by a
    # different desk at a different moment, and the two counts either side of
    # it - waiting to be seen, and seen - are what both screens are built on.
    darshan_at = db.Column(db.DateTime)
    darshan_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    darshan_done_at = db.Column(db.DateTime)
    darshan_done_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    member = db.relationship('Member', overlaps='member_ref,attendance')
    center = db.relationship('Center', overlaps='center_ref,attendance')
    pas = db.relationship('Pass', overlaps='pass,attendance,pass_link,attendance_rows')

    # A person attends once. Without this a member holding both a Yajman and a
    # General pass could check in twice, and the centre summary then reported
    # more attendance than passes - which is what "Passes 1, Attendance 2" was.
    __table_args__ = (
        db.UniqueConstraint('member_id', 'pujan_year', name='uq_attendance_member_year'),
        db.UniqueConstraint('pujan_year', 'seat_no', name='uq_attendance_seat_year'),
    )

    @property
    def seat_label(self):
        return f'{self.seat_no:03d}' if self.seat_no else ''

    def to_dict(self):
        return {
            'id': self.id, 'pass_id': self.pass_id,
            'pass_number': self.pas.pass_number if self.pas else '',
            'member_id': self.member_id,
            'member_name': self.member.display_name if self.member else '',
            'member_code': (self.member.member_code or '') if self.member else '',
            'center_id': self.center_id,
            'center_name': self.center.label if self.center else '',
            'pujan_year': self.pujan_year,
            'seat_no': self.seat_no,
            'seat_label': self.seat_label,
            'slip_printed': bool(self.slip_printed_at),
            'darshan': bool(self.darshan_at),
            'darshan_time': to_ist(self.darshan_at, '%d-%m-%Y %H:%M:%S') or '',
            'darshan_done': bool(self.darshan_done_at),
            'darshan_done_time': to_ist(self.darshan_done_at, '%d-%m-%Y %H:%M:%S') or '',
            'check_in_time': to_ist(self.check_in_time, '%d-%m-%Y %H:%M:%S') or ''
        }


class MessageTemplate(db.Model):
    """A reusable message: what it is for, which channel, which language.

    Kept in the database rather than in code so the wording can be changed for
    a given year without a release, and so several variants can exist side by
    side (a short SMS and a fuller email for the same purpose).
    """
    __tablename__ = 'message_templates'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    name_gu = db.Column(db.String(150), default='')     # shown when the app is in Gujarati
    purpose = db.Column(db.String(20), nullable=False, default='custom')
    # sankalp | pending | event | custom
    channel = db.Column(db.String(20), nullable=False, default='any')
    # sms | whatsapp | email | any
    language = db.Column(db.String(5), nullable=False,
                         default=lambda: DEFAULT_LANGUAGE)     # gu | en
    subject = db.Column(db.String(300), default='')      # email only
    body = db.Column(db.Text, nullable=False, default='')
    include_poster = db.Column(db.Boolean, default=False)
    pujan_year = db.Column(db.Integer)                   # null = every year
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_system = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'name_gu': self.name_gu or '', 'purpose': self.purpose,
            'channel': self.channel, 'language': self.language,
            'subject': self.subject or '', 'body': self.body or '',
            'include_poster': bool(self.include_poster),
            'pujan_year': self.pujan_year,
            'is_active': bool(self.is_active), 'is_system': bool(self.is_system),
            'updated_at': to_ist(self.updated_at, '%d-%m-%Y %H:%M'),
        }


class ScheduledMessage(db.Model):
    """A bulk message set to go out later, once or repeatedly.

    The runner claims a row before sending, so two web workers waking at the
    same moment cannot both send it.
    """
    __tablename__ = 'scheduled_messages'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, default='')
    template_id = db.Column(db.Integer, db.ForeignKey('message_templates.id'), nullable=False)
    channel = db.Column(db.String(20), nullable=False)          # sms|whatsapp|email
    scope = db.Column(db.String(20), nullable=False, default='all')
    years = db.Column(db.Text, default='')                      # JSON list, [] = every year
    center_id = db.Column(db.Integer, db.ForeignKey('centers.id'))
    frequency = db.Column(db.String(20), nullable=False, default='once')
    # once | daily | weekly | monthly | yearly
    next_run_at = db.Column(db.DateTime, nullable=False)
    last_run_at = db.Column(db.DateTime)
    run_count = db.Column(db.Integer, default=0)
    last_result = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    created_at = db.Column(db.DateTime, default=utcnow)

    @property
    def year_list(self):
        try:
            return [int(x) for x in json.loads(self.years or '[]')]
        except (ValueError, TypeError):
            return []

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name,
            'template_id': self.template_id,
            'template_name': self.template.name if self.template else '',
            'channel': self.channel, 'scope': self.scope,
            'years': self.year_list,
            'center_id': self.center_id,
            'center_name': self.center.label if self.center else '',
            'frequency': self.frequency,
            'next_run_at': to_ist(self.next_run_at, '%d-%m-%Y %H:%M'),
            'next_run_iso': self.next_run_at.isoformat() if self.next_run_at else '',
            'last_run_at': to_ist(self.last_run_at, '%d-%m-%Y %H:%M'),
            'run_count': self.run_count or 0,
            'last_result': self.last_result or '',
            'is_active': bool(self.is_active),
        }

    template = db.relationship('MessageTemplate')
    center = db.relationship('Center')


def as_utc(dt):
    """A timezone-aware UTC datetime, whatever the driver handed back.

    Columns are TIMESTAMP without a zone, so SQLite - and Postgres for this
    column type - return naive values. Everything written is UTC, so this
    attaches that rather than guessing.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def advance_schedule(sched, slot=None):
    """Where the next run falls, or None when it was a one-off.

    Counted from the slot it was due rather than from now, so the time of day
    stays put. If several runs were missed - the app was down for a week - it
    rolls forward to the next slot still ahead rather than firing repeatedly.
    """
    freq = sched.frequency
    base = as_utc(slot or sched.next_run_at) or utcnow()

    def step(t):
        if freq == 'daily':
            return t + timedelta(days=1)
        if freq == 'weekly':
            return t + timedelta(weeks=1)
        if freq == 'monthly':
            y, m = t.year + (t.month // 12), (t.month % 12) + 1
            return t.replace(year=y, month=m,
                             day=min(t.day, calendar.monthrange(y, m)[1]))
        if freq == 'yearly':
            try:
                return t.replace(year=t.year + 1)
            except ValueError:                  # 29 February
                return t.replace(year=t.year + 1, day=28)
        return None

    nxt = step(base)
    if nxt is None:
        return None
    now = utcnow()
    guard = 0
    while nxt <= now and guard < 400:
        nxt = step(nxt)
        guard += 1
    return nxt


class Instruction(db.Model):
    """A page a user must read, and accept, before the app opens.

    Who sees it is worked out from the user rather than assigned by hand:
    their type (Team Member or Center), their role, and the rights they hold.
    That way a new user gets the right instructions without anyone remembering
    to attach them.

    `version` is the mechanism for re-consent: raise it and everyone is asked
    again, while their earlier acceptance stays on record.
    """
    __tablename__ = 'instructions'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    title_gu = db.Column(db.String(200), default='')
    body = db.Column(db.Text, nullable=False, default='')
    body_gu = db.Column(db.Text, default='')

    # Who it is for. Empty means everyone.
    audience = db.Column(db.String(20), default='all')      # all|team_member|center
    role_keys = db.Column(db.Text, default='[]')            # JSON list of role keys
    any_perms = db.Column(db.Text, default='[]')            # show if the user holds ANY
    all_perms = db.Column(db.Text, default='[]')            # show only if they hold ALL

    require_accept = db.Column(db.Boolean, default=True, nullable=False)
    version = db.Column(db.Integer, default=1, nullable=False)
    sort_order = db.Column(db.Integer, default=100)
    pujan_year = db.Column(db.Integer)                      # null = every year
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    is_system = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def _list(self, raw):
        try:
            v = json.loads(raw or '[]')
            return [str(x) for x in v] if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []

    @property
    def roles(self):
        return self._list(self.role_keys)

    @property
    def perms_any(self):
        return self._list(self.any_perms)

    @property
    def perms_all(self):
        return self._list(self.all_perms)

    def applies_to(self, user, year=None):
        """Whether this user should be shown this page."""
        if not self.is_active:
            return False
        if self.pujan_year and year and self.pujan_year != year:
            return False
        if self.audience == 'center' and not is_center_scoped(user):
            return False
        if self.audience == 'team_member' and is_center_scoped(user):
            return False
        roles = self.roles
        if roles and (user.role or '') not in roles:
            return False
        p_any = self.perms_any
        if p_any and not any(has_perm(user, k) for k in p_any):
            return False
        p_all = self.perms_all
        if p_all and not all(has_perm(user, k) for k in p_all):
            return False
        return True

    def to_dict(self, lang='en'):
        return {
            'id': self.id,
            'title': (self.title_gu if lang == 'gu' and self.title_gu else self.title),
            'body': (self.body_gu if lang == 'gu' and self.body_gu else self.body),
            'title_en': self.title, 'title_gu': self.title_gu or '',
            'body_en': self.body, 'body_gu': self.body_gu or '',
            'audience': self.audience, 'roles': self.roles,
            'any_perms': self.perms_any, 'all_perms': self.perms_all,
            'require_accept': bool(self.require_accept),
            'version': self.version, 'sort_order': self.sort_order,
            'pujan_year': self.pujan_year,
            'is_active': bool(self.is_active), 'is_system': bool(self.is_system),
            'updated_at': to_ist(self.updated_at, '%d-%m-%Y %H:%M'),
        }


class ManualDoc(db.Model):
    """A user manual served to whoever it is for.

    New admin-uploaded PDFs live on the NAS-backed media bind. Legacy BLOB
    columns remain readable so an existing installation can be migrated in a
    controlled one-time step instead of losing access during cutover.

    Targeting works the same way as the instruction pages: user type, role and
    rights. That is what makes a centre volunteer and a Head Office volunteer
    see different documents without anyone maintaining two lists.
    """
    __tablename__ = 'manuals'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    title_gu = db.Column(db.String(200), default='')
    about = db.Column(db.String(400), default='')
    about_gu = db.Column(db.String(400), default='')
    filename = db.Column(db.String(200), nullable=False, default='manual.pdf')
    mime = db.Column(db.String(80), default='application/pdf')
    data = db.Column(db.LargeBinary)  # legacy fallback; new uploads use file_path
    file_path = db.Column(db.String(500), default='')
    size = db.Column(db.Integer, default=0)
    # The same manual in Gujarati, as its own file. Not a translated title over
    # an English PDF: a volunteer who reads Gujarati needs the pages in
    # Gujarati, and a document cannot be translated on the fly. Where no
    # Gujarati file has been uploaded the English one is served instead, so a
    # manual always opens.
    filename_gu = db.Column(db.String(200), default='')
    data_gu = db.Column(db.LargeBinary)  # legacy fallback; new uploads use file_path_gu
    file_path_gu = db.Column(db.String(500), default='')
    size_gu = db.Column(db.Integer, default=0)
    version_gu = db.Column(db.String(40), default='')
    # The manual as plain prose, one page per line, for reading aloud.
    #
    # Kept beside the PDF rather than lifted out of it. A Gujarati PDF stores
    # shaped glyphs in visual order - matras have already been moved to where
    # they are drawn - so text pulled back out comes to a speech voice
    # reordered and partly unmapped: fine to look at, useless to listen to.
    # This is the same words in the order they were written, with page numbers
    # and footers left out, which reads better aloud in either language.
    speech_en = db.Column(db.Text, default='')
    speech_gu = db.Column(db.Text, default='')

    audience = db.Column(db.String(20), default='all')      # all|team_member|center
    role_keys = db.Column(db.Text, default='[]')
    any_perms = db.Column(db.Text, default='[]')

    sort_order = db.Column(db.Integer, default=100)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    version = db.Column(db.String(40), default='')
    uploaded_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    uploaded_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def _list(self, raw):
        try:
            v = json.loads(raw or '[]')
            return [str(x) for x in v] if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []

    @property
    def roles(self):
        return self._list(self.role_keys)

    @property
    def perms_any(self):
        return self._list(self.any_perms)

    def applies_to(self, user):
        if not self.is_active:
            return False
        if self.audience == 'center' and not is_center_scoped(user):
            return False
        if self.audience == 'team_member' and is_center_scoped(user):
            return False
        roles = self.roles
        if roles and (user.role or '') not in roles:
            return False
        p_any = self.perms_any
        if p_any and not any(has_perm(user, k) for k in p_any):
            return False
        return True

    def to_dict(self, lang='en'):
        return {
            'id': self.id,
            'title': (self.title_gu if lang == 'gu' and self.title_gu else self.title),
            'about': (self.about_gu if lang == 'gu' and self.about_gu else self.about),
            'title_en': self.title, 'title_gu': self.title_gu or '',
            'about_en': self.about, 'about_gu': self.about_gu or '',
            'filename': self.filename, 'mime': self.mime,
            'size_kb': round((self.size or 0) / 1024),
            'has_gu': bool(self.file_path_gu or self.data_gu),
            'size_gu_kb': round((self.size_gu or 0) / 1024),
            'version_gu': self.version_gu or '',
            # Which file this reader will actually be given, so the dialog can
            # say so rather than leaving them to find out.
            'serves_gu': bool(self.file_path_gu or self.data_gu) and lang == 'gu',
            'audience': self.audience, 'roles': self.roles,
            'any_perms': self.perms_any,
            'sort_order': self.sort_order, 'is_active': bool(self.is_active),
            'version': self.version or '',
            'has_file': bool(self.file_path or self.data),
            'uploaded_at': to_ist(self.uploaded_at, '%d-%m-%Y %H:%M'),
        }


class InstructionAcceptance(db.Model):
    """Who accepted what, and when. Kept per version, never overwritten."""
    __tablename__ = 'instruction_acceptances'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    instruction_id = db.Column(db.Integer, db.ForeignKey('instructions.id'), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    accepted_at = db.Column(db.DateTime, default=utcnow)
    ip = db.Column(db.String(60))
    __table_args__ = (
        db.UniqueConstraint('user_id', 'instruction_id', 'version',
                            name='uq_accept_user_instruction_version'),
    )
    user = db.relationship('User')
    instruction = db.relationship('Instruction')


class CommLog(db.Model):
    __tablename__ = 'comm_logs'
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=True)
    comm_type = db.Column(db.String(10), nullable=False)
    destination = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(300))
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='queued')
    error = db.Column(db.Text)
    pujan_year = db.Column(db.Integer)
    audience = db.Column(db.String(20))          # 'center' | 'ho'
    template_id = db.Column(db.Integer, db.ForeignKey('message_templates.id'))
    sent_at = db.Column(db.DateTime, default=utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'member_id': self.member_id, 'comm_type': self.comm_type,
            'destination': self.destination, 'subject': self.subject or '',
            'status': self.status, 'error': self.error or '',
            'audience': self.audience or '', 'pujan_year': self.pujan_year,
            'sent_at': to_ist(self.sent_at),
        }


DEFAULT_TEMPLATES = {
    'center_gu': (
        "જય સ્વામિનારાયણ {name},\n\n"
        "SMVS ચોપડા પૂજન {year} — તા. {date}, {weekday}, સમય {time}.\n"
        "સ્થળ: {venue}\n\n"
        "આપનું નામ {center} સેન્ટરમાં નોંધાયેલ છે. કૃપા કરી ધોતી-ઉપરણી પહેરીને "
        "સમયસર પધારશો.\n\n"
        "પૂજન સામગ્રી: {samagri}\n\n"
        "સંપર્ક: {contact}\n— SMVS"),
    'center_en': (
        "Jay Swaminarayan {name},\n\n"
        "SMVS Chopda-Pujan {year} — {date}, {weekday}, {time}.\n"
        "Venue: {venue}\n\n"
        "You are registered at {center} Center. Please wear dhoti-uparni and "
        "arrive on time.\n\n"
        "Items to bring: {samagri}\n\n"
        "Contact: {contact}\n— SMVS"),
    'ho_gu': (
        "જય સ્વામિનારાયણ {name},\n\n"
        "SMVS ચોપડા પૂજન {year} — તા. {date}, {weekday}, સમય {time}.\n"
        "આપ હેડ ઓફિસ ખાતે લાભ લેવાના છો.\n"
        "સ્થળ: {ho_venue}\n{ho_address}\n\n"
        "આપનું સેન્ટર: {center} | પાસ નંબર: {pass_number}\n"
        "કૃપા કરી પાસ સાથે રાખવો અને {report_time} વાગ્યે પહોંચવું.\n\n"
        "પૂજન સામગ્રી: {samagri}\n\n"
        "સંપર્ક: {contact}\n— SMVS"),
    'ho_en': (
        "Jay Swaminarayan {name},\n\n"
        "SMVS Chopda-Pujan {year} — {date}, {weekday}, {time}.\n"
        "You are attending at Head Office.\n"
        "Venue: {ho_venue}\n{ho_address}\n\n"
        "Your center: {center} | Pass number: {pass_number}\n"
        "Please carry your pass and report by {report_time}.\n\n"
        "Items to bring: {samagri}\n\n"
        "Contact: {contact}\n— SMVS"),
}

DEFAULT_SAMAGRI_GU = ("ચોપડા, આસન, થાળી, વાટકી, લોટો, ચમચી, શ્રીફળ, નાડાછેડી, "
                      "અબીલ-ગુલાલ-કંકુ, ચોખા, પડિયા, સોપારી, નાગરવેલના પાન, "
                      "દિવેટ, માચીસ, સાકર ૫૦૦ ગ્રામ, પુષ્પ")
DEFAULT_SAMAGRI_EN = ("Chopda, aasan, thali, vaatki, loto, spoon, shrifal, "
                      "nada-chhedi, abil-gulal-kanku, rice, padiya, sopari, "
                      "nagarvel leaves, divet, matches, 500g sugar, flower")


class MemberRequest(db.Model):
    """Something a member asked to change about their own record.

    Every submission is written down, including the ones applied straight
    away. One place to look afterwards beats "it went in, but I cannot show
    you who asked for it" - and it makes turning the auto setting off later a
    change of policy rather than a change of history.
    """
    __tablename__ = 'member_requests'
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('members.id'), nullable=False)
    pujan_year = db.Column(db.Integer, nullable=False)
    kind = db.Column(db.String(20), nullable=False)        # edit_firm | add_firm
    entity_id = db.Column(db.Integer, db.ForeignKey('entities.id'))
    entity_type = db.Column(db.String(20), default='firm')  # for add_firm
    old_name = db.Column(db.String(200), default='')
    new_name = db.Column(db.String(200), default='')
    status = db.Column(db.String(20), default='pending')   # pending|applied|rejected
    auto = db.Column(db.Boolean, default=False, nullable=False)
    from_ip = db.Column(db.String(60), default='')
    created_at = db.Column(db.DateTime, default=utcnow)
    decided_at = db.Column(db.DateTime)
    decided_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    member = db.relationship('Member')

    def to_dict(self):
        m = self.member
        return {
            'id': self.id, 'member_id': self.member_id,
            'member_name': m.display_name if m else '',
            'member_code': (m.member_code or '') if m else '',
            'center_name': (m.center.label if m and m.center else ''),
            'pujan_year': self.pujan_year, 'kind': self.kind,
            'entity_id': self.entity_id, 'entity_type': self.entity_type or 'firm',
            'old_name': self.old_name or '', 'new_name': self.new_name or '',
            'status': self.status, 'auto': bool(self.auto),
            'from_ip': self.from_ip or '',
            'created_at': to_ist(self.created_at, '%d-%m-%Y %H:%M') or '',
            'decided_at': to_ist(self.decided_at, '%d-%m-%Y %H:%M') or '',
        }


class EventConfig(db.Model):
    """Everything about one year's pujan that changes year to year.

    Held in the database rather than the code so an admin can update the date,
    the venue, the wording and the poster without a redeploy.
    """
    __tablename__ = 'event_config'
    id = db.Column(db.Integer, primary_key=True)
    pujan_year = db.Column(db.Integer, nullable=False, unique=True)
    event_date = db.Column(db.Date)
    event_time = db.Column(db.String(50), default='7:30 AM to 10:00 AM')
    report_time = db.Column(db.String(50), default='7:00 AM')
    # Two places, because the event happens in two: the head centre, and
    # whichever centre the member belongs to.
    #
    #   ho_venue / ho_address   the head centre, one address for everybody
    #                           marked as attending there
    #   center_venue            wording used for everybody else. Left empty,
    #                           each member is told the name of the centre
    #                           they are registered at, which is what they
    #                           actually need to read.
    ho_venue = db.Column(db.String(200), default='SMVS Swaminarayan Mandir, Vasna')
    ho_address = db.Column(db.Text, default='')
    center_venue = db.Column(db.String(200), default='')
    contact_phone = db.Column(db.String(100), default='')
    language = db.Column(db.String(5),
                         default=lambda: DEFAULT_LANGUAGE)     # gu | en
    samagri_gu = db.Column(db.Text, default=DEFAULT_SAMAGRI_GU)
    samagri_en = db.Column(db.Text, default=DEFAULT_SAMAGRI_EN)
    body_center_gu = db.Column(db.Text, default=DEFAULT_TEMPLATES['center_gu'])
    body_center_en = db.Column(db.Text, default=DEFAULT_TEMPLATES['center_en'])
    body_ho_gu = db.Column(db.Text, default=DEFAULT_TEMPLATES['ho_gu'])
    body_ho_en = db.Column(db.Text, default=DEFAULT_TEMPLATES['ho_en'])
    subject_gu = db.Column(db.String(200), default='SMVS ચોપડા પૂજન {year}')
    subject_en = db.Column(db.String(200), default='SMVS Chopda-Pujan {year}')
    # New poster uploads live on the NAS-backed media bind. poster_data remains
    # only as a backwards-compatible fallback for databases from older builds.
    poster_data = db.Column(db.LargeBinary)
    poster_path = db.Column(db.String(500), default='')
    poster_mime = db.Column(db.String(80))
    poster_name = db.Column(db.String(200))
    # Letting a member correct their own firm name, from a link in the
    # message, instead of ringing the centre and having somebody type it in.
    #
    #   selfserve_open      the whole thing on or off for this year
    #   selfserve_until     the last date the link works. Left empty it never
    #                       closes, which is the one setting worth thinking
    #                       about: a name changing after the passes are
    #                       printed leaves the gate with two versions of it.
    #   selfserve_auto_*    whether a submission is written straight away or
    #                       waits in the review queue. Separate for adding and
    #                       editing, because an added firm can be removed
    #                       later while an edit has already overwritten what a
    #                       volunteer entered.
    selfserve_open = db.Column(db.Boolean, default=True, nullable=False)
    selfserve_until = db.Column(db.Date)
    selfserve_auto_add = db.Column(db.Boolean, default=True, nullable=False)
    selfserve_auto_edit = db.Column(db.Boolean, default=True, nullable=False)
    selfserve_auto_email = db.Column(db.Boolean, default=True, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def to_dict(self):
        return {
            'pujan_year': self.pujan_year,
            'event_date': self.event_date.strftime('%d-%m-%Y') if self.event_date else '',
            'event_date_iso': self.event_date.strftime('%Y-%m-%d') if self.event_date else '',
            'weekday': self.event_date.strftime('%A') if self.event_date else '',
            'event_time': self.event_time or '', 'report_time': self.report_time or '',
            'ho_venue': self.ho_venue or '', 'ho_address': self.ho_address or '',
            'center_venue': self.center_venue or '',
            'contact_phone': self.contact_phone or '',
            'language': self.language or DEFAULT_LANGUAGE,
            'samagri_gu': self.samagri_gu or '', 'samagri_en': self.samagri_en or '',
            'body_center_gu': self.body_center_gu or '', 'body_center_en': self.body_center_en or '',
            'body_ho_gu': self.body_ho_gu or '', 'body_ho_en': self.body_ho_en or '',
            'subject_gu': self.subject_gu or '', 'subject_en': self.subject_en or '',
            'selfserve_open': bool(self.selfserve_open),
            'selfserve_until': (self.selfserve_until.isoformat()
                                if self.selfserve_until else ''),
            'selfserve_auto_add': bool(self.selfserve_auto_add),
            'selfserve_auto_edit': bool(self.selfserve_auto_edit),
            'selfserve_auto_email': bool(self.selfserve_auto_email),
            'has_poster': bool(self.poster_path or self.poster_data),
            'poster_name': self.poster_name or '',
            'updated_at': to_ist(self.updated_at),
        }


class PassConfig(db.Model):
    __tablename__ = 'pass_config'
    id = db.Column(db.Integer, primary_key=True)
    pujan_year = db.Column(db.Integer, nullable=False, unique=True)
    # The three ranges deliberately do not overlap, which is what lets the
    # check-in desk type a bare number and have it resolve to one pass.
    yajman_start = db.Column(db.Integer, default=501)
    guruji_start = db.Column(db.Integer, default=1001)
    general_start = db.Column(db.Integer, default=1501)
    yajman_prefix = db.Column(db.String(10), default='YJ')
    guruji_prefix = db.Column(db.String(10), default='GU')
    general_prefix = db.Column(db.String(10), default='GN')
    updated_by = db.Column(db.Integer, db.ForeignKey('users.id'))
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    def start_for(self, ptype):
        return {'yajman': self.yajman_start, 'guruji': self.guruji_start,
                'general': self.general_start}.get(ptype, 1)

    def prefix_for(self, ptype):
        return {'yajman': self.yajman_prefix, 'guruji': self.guruji_prefix,
                'general': self.general_prefix}.get(ptype, 'PS')

    def to_dict(self):
        return {
            'id': self.id, 'pujan_year': self.pujan_year,
            'yajman_start': self.yajman_start, 'guruji_start': self.guruji_start,
            'general_start': self.general_start,
            'yajman_prefix': self.yajman_prefix, 'guruji_prefix': self.guruji_prefix,
            'general_prefix': self.general_prefix,
        }


# ======================== HELPERS ========================
def _parse_date(value):
    """Accepts dd-mm-yyyy (what the UI shows) or yyyy-mm-dd (what a date input
    posts), so neither side has to care which is which."""
    v = str(value or '').strip()
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f'"{value}" is not a date')


PASS_TYPES = ('yajman', 'guruji', 'general')


def get_current_year():
    return datetime.now().year

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'Please login', 'code': 401}), 401
        return f(*args, **kwargs)
    return decorated

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if 'user_id' not in session:
                return jsonify({'error': 'Please login', 'code': 401}), 401
            user = User.query.get(session['user_id'])
            if not user or user.role not in roles:
                return jsonify({'error': 'Permission denied', 'code': 403}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

# Centre-level staff. center_user was excluded from most write routes, which
# is why that role could see the pages but do nothing on them.
CENTER_STAFF = ('super_admin', 'admin', 'center_sant', 'center_user')

# ======================== PERMISSIONS ========================
# The menu and the buttons are driven by these keys rather than hard-coded to
# a role. A role supplies the defaults; an admin can then switch any single
# permission on or off for one user, and both the sidebar and the API follow.
PERMISSION_GROUPS = [
    ('pages', 'Pages', [
        ('page.dashboard', 'Dashboard'),
        ('page.centers', 'Centers'),
        ('page.members', 'Members'),
        ('page.sankalps', 'Sankalp'),
        ('page.collections', 'Collection'),
        ('page.passes', 'Passes'),
        ('page.attendance', 'Attendance'),
        ('page.reports', 'Reports'),
        ('page.users', 'Users'),
        ('page.communication', 'Communication'),
        ('page.audit', 'Activity Log'),
    ]),
    ('members', 'Members', [
        ('member.create', 'Add a member (full form)'),
        ('member.fast_entry', 'Fast Entry for members'),
        ('member.edit', 'Edit a member'),
        ('member.delete', 'Delete a member'),
        ('member.ho', 'Mark who attends at Head Office'),
        ('member.requests', 'Review what members ask to change'),
    ]),
    ('sankalps', 'Sankalp', [
        ('sankalp.create', 'Add a sankalp (full form)'),
        ('sankalp.fast_entry', 'Fast Entry for sankalp'),
        ('sankalp.edit', 'Revise a sankalp amount'),
        ('sankalp.delete', 'Delete a sankalp'),
        ('sankalp.carry_forward', 'Carry forward to another year'),
        ('sankalp.bulk_edit', 'Bulk edit sankalp amounts'),
        # Filed under Sankalp rather than with check-in: the desk holding it
        # works on the Sankalp screen, confirming pledges, and this is where
        # whoever grants it goes looking.
        ('attendance.darshan', 'Guruji Darshan'),
    ]),
    ('collections', 'Collection', [
        ('collection.create', 'Enter a collection'),
        ('collection.delete', 'Delete a collection'),
    ]),
    ('event', 'Passes & Attendance', [
        ('pass.generate', 'Generate passes'),
        ('pass.config', 'Change pass numbering'),
        ('attendance.checkin', 'Check people in'),
        ('attendance.list', 'See the attendance list'),
        ('slip.print', 'Print the check-in seat slips'),
        ('pass.print_card', 'Print passes on card size'),
        ('pass.print_member', 'Print the member copy of a pass'),
        ('pass.print_office', 'Print the office copy (shows amounts)'),
    ]),
    ('finance', 'Money', [
        ('finance.sankalp', 'See sankalp amounts'),
        ('finance.collected', 'See collected amounts'),
        ('finance.pending', 'See pending amounts'),
        ('finance.dharmada', 'See dharmada amounts'),
    ]),
    ('admin', 'Administration', [
        ('center.manage', 'Add and edit centers'),
        ('user.manage', 'Add and edit users'),
        ('data.export', 'Export CSV'),
        ('data.import', 'Import CSV'),
        ('notify.email', 'Send the instruction email'),
        ('notify.whatsapp', 'Send WhatsApp messages'),
        ('notify.sms', 'Send SMS'),
        ('comm.templates_view', 'See message templates'),
        ('comm.templates', 'Add and edit message templates'),
        ('comm.templates_delete', 'Delete message templates'),
        ('comm.schedules', 'Manage scheduled messages'),
        ('comm.log', 'See sent messages'),
        ('instructions.edit', 'Write the instruction pages users must accept'),
        ('instructions.report', 'See who has accepted the instructions'),
        ('manual.view', 'Open the user manual'),
        ('manual.manage', 'Upload and replace the user manual'),
        ('event.config', 'Edit event details and messages'),
    ]),
]

ALL_PERMISSIONS = [k for _, _, items in PERMISSION_GROUPS for k, _ in items]

_ALL_ON = {k: True for k in ALL_PERMISSIONS}


def _perms(*on):
    """Everything off except the keys listed."""
    d = {k: False for k in ALL_PERMISSIONS}
    for k in on:
        d[k] = True
    return d


ROLE_PERMISSIONS = {
    'super_admin': dict(_ALL_ON),
    'admin': {**_ALL_ON, 'page.users': False, 'user.manage': False},
    'center_sant': _perms(
        'page.dashboard', 'page.members', 'page.sankalps', 'page.collections',
        'page.passes', 'page.attendance', 'page.reports',
        'member.create', 'member.fast_entry', 'member.edit', 'member.delete',
        'member.ho', 'member.requests',
        'sankalp.create', 'sankalp.fast_entry', 'sankalp.edit', 'sankalp.delete',
        'sankalp.bulk_edit',
        'finance.sankalp', 'finance.collected', 'finance.pending', 'finance.dharmada',
        'collection.create', 'collection.delete',
        'pass.generate', 'pass.print_card', 'pass.print_member', 'pass.print_office',
        'attendance.checkin', 'attendance.darshan', 'attendance.list',
        'slip.print', 'data.export'),
    'center_user': _perms(
        'page.dashboard', 'page.members', 'page.sankalps', 'page.collections',
        'page.reports',
        'member.create', 'member.fast_entry', 'member.edit', 'member.ho',
        'sankalp.create', 'sankalp.fast_entry', 'sankalp.edit',
        'collection.create',
        'finance.sankalp', 'finance.collected', 'finance.pending', 'finance.dharmada',
        'data.export'),
}


def get_role(key):
    return Role.query.filter_by(key=key).first() if key else None


def role_defaults(key):
    """The role's permission map, falling back to the built-in defaults if the
    roles table has not been seeded yet."""
    r = get_role(key)
    return r.perm_map if r else dict(ROLE_PERMISSIONS.get(key, _perms()))


def is_super(user):
    return bool(user) and user.role == 'super_admin'


def is_center_scoped(user):
    """Whether this user only ever sees one centre.

    The user's own type wins, because the same role can be held by someone at
    Head Office and by someone at a centre. Older rows have no type, and there
    the role's own scope still decides.
    """
    if not user or is_super(user):
        return False
    if user.user_type == 'team_member':
        return False
    if user.user_type == 'center':
        return bool(user.center_id)
    r = get_role(user.role)
    if r:
        return bool(r.center_scoped) and bool(user.center_id)
    return user.role in ('center_sant', 'center_user') and bool(user.center_id)


def default_user_type(role_key):
    r = get_role(role_key)
    if r:
        return 'center' if r.center_scoped else 'team_member'
    return 'center' if role_key in ('center_sant', 'center_user') else 'team_member'


def effective_permissions(user):
    """The role's permissions, with this user's own overrides on top."""
    if is_super(user):
        return dict(_ALL_ON)                  # never lock out the super admin
    base = dict(role_defaults(user.role))
    try:
        overrides = json.loads(user.permissions) if user.permissions else {}
    except (ValueError, TypeError):
        overrides = {}
    for k, v in (overrides or {}).items():
        if k in base:
            base[k] = bool(v)
    return base


def has_perm(user, key):
    if not user:
        return False
    return effective_permissions(user).get(key, False)


def perm_required(*keys):
    """Any one of the listed permissions is enough."""
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not session.get('user_id'):
                return jsonify({'error': 'Please login', 'code': 401}), 401
            u = User.query.get(session['user_id'])
            if not u or not u.is_active:
                return jsonify({'error': 'Please login', 'code': 401}), 401
            if not any(has_perm(u, k) for k in keys):
                return jsonify({'error': 'You do not have permission for that. '
                                         'Ask an admin to enable it.'}), 403
            return f(*a, **kw)
        return wrapper
    return deco

CENTER_ROLES = ('center_sant', 'center_user')


AUDIT_FIELDS = {
    'member': ('member_code', 'name', 'member_type', 'center_id', 'mobile',
               'email', 'dharmada_type', 'dharmada_amount', 'address',
               'is_active', 'attend_at_ho'),
    'sankalp': ('member_id', 'pujan_year', 'sankalp_type', 'sankalp_name',
                'amount', 'entity_id', 'person_id', 'center_id', 'owner_center_id'),
    'collection': ('sankalp_id', 'amount', 'collection_date', 'center_id', 'remarks'),
    'center': ('name', 'name_en', 'city', 'phone', 'email', 'is_active'),
    'user': ('username', 'full_name', 'role', 'user_type', 'center_id', 'mobile',
             'email', 'is_active'),
    'pass': ('pass_number', 'pass_type', 'member_id', 'center_id', 'pujan_year'),
    'attendance': ('member_id', 'center_id', 'pujan_year', 'seat_no'),
}


def snapshot(obj, kind):
    """The audited fields of a row, as plain JSON-safe values."""
    if obj is None:
        return {}
    out = {}
    for f in AUDIT_FIELDS.get(kind, ()):
        v = getattr(obj, f, None)
        if isinstance(v, Decimal):
            v = float(v)
        elif hasattr(v, 'strftime'):
            v = v.strftime('%d-%m-%Y')
        out[f] = v
    return out


def audit(action, kind=None, obj=None, old=None, new=None, label=None, year=None):
    """Record one action. Never raises - an audit failure must not break the
    operation it is describing."""
    try:
        u = User.query.get(session['user_id']) if session.get('user_id') else None
        ip = (request.headers.get('X-Forwarded-For', request.remote_addr or '')
              .split(',')[0].strip()) if request else ''
        if obj is not None and new is None and action != 'delete':
            new = snapshot(obj, kind)
        if obj is not None and action == 'delete' and old is None:
            old = snapshot(obj, kind)
        db.session.add(AuditLog(
            user_id=u.id if u else None,
            username=u.username if u else '',
            full_name=u.full_name if u else '',
            role=u.role if u else '',
            center_id=u.center_id if u else None,
            action=action, entity_type=kind,
            entity_id=getattr(obj, 'id', None),
            entity_label=(label or getattr(obj, 'display_name', None)
                          or getattr(obj, 'name', None) or getattr(obj, 'username', None) or '')[:300],
            pujan_year=year or getattr(obj, 'pujan_year', None),
            old_values=json.dumps(old, default=str) if old else None,
            new_values=json.dumps(new, default=str) if new else None,
            ip=ip))
    except Exception as e:
        print(f"[audit] skipped: {e.__class__.__name__}: {e}")


MONEY_KEYS = ('amount', 'collected', 'pending', 'dharmada_amount',
              'sankalp_amount', 'last_year_amount', 'pending_amount')


def money_visibility(user=None):
    """Which of the four figures this user may see."""
    user = user or User.query.get(session.get('user_id') or 0)
    return {k: has_perm(user, 'finance.' + k)
            for k in ('sankalp', 'collected', 'pending', 'dharmada')}


def strip_money(payload, show=None):
    """Blanks every money figure the user has no right to, at any depth.

    Walking the whole structure rather than named top-level fields, because the
    figures also sit inside nested lists - a member's `sankalps`, a pass's
    `sankalp_lines`, previous-year history - and stripping only the top level
    left those readable to anyone who opened the network tab.
    """
    show = show if show is not None else money_visibility()
    if all(show.values()):
        return payload

    which = {
        'amount': 'sankalp', 'sankalp_amount': 'sankalp', 'last_year_amount': 'sankalp',
        'last_amount': 'sankalp',
        'collected': 'collected', 'last_collected': 'collected',
        'pending': 'pending', 'pending_amount': 'pending', 'last_pending': 'pending',
        'dharmada_amount': 'dharmada',
    }

    def walk(node):
        if isinstance(node, list):
            return [walk(x) for x in node]
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k in which and not show[which[k]]:
                    out[k] = None
                else:
                    out[k] = walk(v)
            return out
        return node

    result = walk(payload)
    if isinstance(result, dict):
        result['money_shown'] = show
    return result


def guard_center(obj, what='record'):
    """Refuses when this row belongs to another centre.

    Every handler that takes an id from the URL has to ask this: the id is
    guessable, so without it a centre user could read or change a neighbouring
    centre's data just by changing the number.
    """
    cf = get_user_center_filter()
    if cf and getattr(obj, 'center_id', None) != cf:
        return jsonify({'error': f'That {what} belongs to another center'}), 403
    return None


def get_user_center_filter():
    """The centre a user is restricted to, or None for a head-office role.

    Driven by the role's center_scoped flag rather than a hard-coded list, so
    a custom centre-level role is scoped exactly like the built-in ones.
    """
    user = User.query.get(session.get('user_id'))
    if not user:
        return None
    return user.center_id if is_center_scoped(user) else None

def send_notification(member, message_text, year=None):
    """The pledge confirmation - actually sent, through whichever gateways are
    configured.

    This used to write status='sent' having sent nothing at all, which made the
    log worse than useless: it said a member had been told when they had not.
    Now each row carries what really happened - sent, failed with the gateway's
    own reason, or queued when there is no gateway for that channel.

    Never allowed to raise. A pledge must save even when the SMS gateway is
    down; the failure belongs in the log, not in the way of the entry.
    """
    year = year or get_current_year()
    logs = []

    def record(channel, dest, subject=''):
        log = CommLog(member_id=member.id, comm_type=channel, destination=dest,
                      subject=subject, message=message_text, status='queued',
                      pujan_year=year,
                      audience='ho' if member.attend_at_ho else 'center')
        db.session.add(log)
        return log

    try:
        if member.mobile:
            log = record('sms', member.mobile)
            logs.append('SMS')
            if notify.sms_configured():
                res = notify.send_sms_via_provider(
                    [{'phone': member.mobile, 'message': message_text}])[0]
                log.status = 'sent' if res.get('ok') else 'failed'
                log.error = res.get('error')
            else:
                log.error = 'No SMS gateway configured'

        if member.email:
            log = record('email', member.email, 'SMVS Chopda-Pujan')
            logs.append('EMAIL')
            if notify.email_configured():
                res = notify.send_emails(
                    [{'to': member.email, 'subject': 'SMVS Chopda-Pujan',
                      'body': message_text}])[0]
                log.status = 'sent' if res.get('ok') else 'failed'
                log.error = res.get('error')
            else:
                log.error = 'No SMTP server configured'

        if logs:
            db.session.commit()
    except Exception as e:
        # The rows are still worth keeping, marked as failed.
        db.session.rollback()
        app.logger.warning('Confirmation to member %s failed: %s', member.id, e)
        try:
            for channel, dest in (('sms', member.mobile), ('email', member.email)):
                if dest:
                    log = record(channel, dest)
                    log.status = 'failed'
                    log.error = f'{e.__class__.__name__}: {e}'[:400]
            db.session.commit()
        except Exception:
            db.session.rollback()
    return logs


from flask_wtf.csrf import CSRFError


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    return jsonify({'error': 'Your session expired. Reload the page and try again.'}), 400


@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Not found'}), 404
    # An image or file URL must answer 404, not a redirect to the app: a mail
    # client following a bad QR link would otherwise be handed a page of HTML
    # and show a broken image with no explanation.
    if request.path.startswith(('/qr/', '/static/')) or '.' in request.path.rsplit('/', 1)[-1]:
        return 'Not found', 404
    return redirect(url_for('index'))


@app.errorhandler(500)
def handle_500(e):
    db.session.rollback()
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Server error. Please try again.'}), 500
    return 'Server error', 500


# ======================== PAGE ROUTES ========================
@app.route('/')
def index():
    if 'user_id' in session: return redirect(url_for('app_page'))
    return redirect(url_for('login_page'))

@app.errorhandler(Exception)
def handle_unexpected(e):
    """An unhandled error on an API route used to come back as an HTML 500,
    which the page could only report as "Something went wrong". It now returns
    JSON naming the failure, so the message on screen is the actual reason.
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if request.path.startswith('/api/'):
            return jsonify({'error': e.description, 'code': e.code}), e.code
        return e
    db.session.rollback()
    app.logger.exception('Unhandled error on %s %s', request.method, request.path)
    if request.path.startswith('/api/'):
        return jsonify({'error': f'{e.__class__.__name__}: {e}',
                        'where': f'{request.method} {request.path}'}), 500
    raise e


@app.route('/healthz')
@csrf.exempt
def healthz():
    """Container healthcheck. Confirms the database round-trips, not just
    that the Python process is alive."""
    try:
        db.session.execute(db.text('SELECT 1'))
        return jsonify({'status': 'ok'}), 200
    except Exception:
        db.session.rollback()
        return jsonify({'status': 'db_unavailable'}), 503


@app.route('/login')
def login_page():
    if 'user_id' in session: return redirect(url_for('app_page'))
    return render_template('login.html')

@app.route('/app')
@login_required
def app_page():
    user = User.query.get(session['user_id'])
    return render_template('app.html', user=user.to_dict(),
                           default_language=DEFAULT_LANGUAGE)

@app.route('/passes/print')
@login_required
def passes_print_page():
    """Dedicated print view. The in-app modal cannot be printed because the
    print stylesheet (correctly) hides modal overlays."""
    cf = get_user_center_filter()
    year = request.args.get('year', get_current_year(), type=int)
    q = Pass.query.filter_by(pujan_year=year)
    if cf:
        q = q.filter_by(center_id=cf)
    cid = request.args.get('center_id', type=int)
    if cid:
        q = q.filter_by(center_id=cid)
    ptype = request.args.get('pass_type')
    if ptype:
        q = q.filter_by(pass_type=ptype)
    ids = request.args.get('ids')
    if ids:
        try:
            q = q.filter(Pass.id.in_([int(x) for x in ids.split(',') if x.strip()]))
        except ValueError:
            pass
    rows = q.order_by(Pass.pass_number).all()
    passes = [p.to_dict() for p in rows]

    # Seat numbers come from the check-in, so a pass printed after someone has
    # arrived carries their seat.
    seats = {a.member_id: a for a in Attendance.query.filter_by(pujan_year=year).all()}
    for d, p in zip(passes, rows):
        att = seats.get(p.member_id)
        d['seat_no'] = att.seat_no if att else None
        d['seat_label'] = att.seat_label if att else ''
        # Last year's pledge and this year's outstanding, for the office copy.
        prev = Sankalp.query.filter_by(member_id=p.member_id,
                                       pujan_year=year - 1).all()
        d['last_year'] = year - 1
        d['last_year_amount'] = float(sum(Decimal(str(x.amount)) for x in prev)) if prev else 0.0
        # A line can have no amount at all - someone registered at the door
        # with their firms but no pledge yet - so skip those rather than
        # trying to add None.
        d['pending_amount'] = float(sum(Decimal(str(l['pending']))
                                        for l in d.get('sankalp_lines') or []
                                        if l.get('pending') is not None))
        d['dharmada_type'] = (p.member.dharmada_type or '') if p.member else ''
        d['dharmada_amount'] = float(p.member.dharmada_amount or 0) if p.member else 0.0

    me = User.query.get(session['user_id'])

    # Remember that these went to the printer.
    if request.args.get('mark') != '0':
        for p in rows:
            p.print_count = (p.print_count or 0) + 1
            p.last_printed_at = utcnow()
            p.last_printed_by = me.id
        db.session.commit()

    if request.args.get('size') == 'wide':
        # Which copies this user may print. Two people can share the job: one
        # prints member copies, another the office copies with the amounts.
        allowed = []
        if has_perm(me, 'pass.print_member'):
            allowed.append('member')
        if has_perm(me, 'pass.print_office'):
            allowed.append('office')
        want = request.args.get('copy', 'both')
        if want in ('member', 'office'):
            allowed = [x for x in allowed if x == want]
        if not allowed:
            return jsonify({'error': 'You do not have permission to print '
                                     'these pass copies.'}), 403
        return render_template('passes_wide.html', passes=passes, year=year,
                               copies=allowed, event=get_event_config(year))

    if not has_perm(me, 'pass.print_card'):
        return jsonify({'error': 'You do not have permission to print '
                                 'card-size passes.'}), 403
    # money=0 prints the card with the figures left off, for handing out. The
    # firm names stay - they are how the holder knows the pass is theirs - so
    # only the amounts go.
    show_money = request.args.get('money') != '0'
    if not show_money:
        for d in passes:
            d['sankalp_amount'] = None
            for ln in d.get('sankalp_lines') or []:
                ln['amount'] = None
                ln['collected'] = None
                ln['pending'] = None
    return render_template('passes_print.html', passes=passes, year=year,
                           show_money=show_money)


@app.route('/logout')
def logout():
    if session.get('user_id'):
        u = User.query.get(session['user_id'])
        if u:
            audit('logout', 'user', u, new={}, label=u.full_name or u.username)
            db.session.commit()
    session.clear()
    return redirect(url_for('index'))


# ======================== API: AUTH ========================
_LOGIN_ATTEMPTS = {}          # {ip: [failure_count, first_failure_at]}
_LOGIN_MAX = 8
_LOGIN_WINDOW = timedelta(minutes=15)


def _login_blocked(ip):
    rec = _LOGIN_ATTEMPTS.get(ip)
    if not rec:
        return False
    count, first = rec
    if utcnow() - first > _LOGIN_WINDOW:
        _LOGIN_ATTEMPTS.pop(ip, None)
        return False
    return count >= _LOGIN_MAX


def _login_failed(ip):
    count, first = _LOGIN_ATTEMPTS.get(ip, (0, utcnow()))
    if utcnow() - first > _LOGIN_WINDOW:
        count, first = 0, utcnow()
    _LOGIN_ATTEMPTS[ip] = (count + 1, first)


def rate_ok(key, limit, minutes):
    """True while `key` has been used fewer than `limit` times in `minutes`.

    Counts every use, not just failures - the login throttle above counts
    failures because a correct password should never be punished, but a public
    write path is different: a forwarded link should not become a way to
    rewrite a record over and over, whether or not each attempt succeeds.

    In memory, so a restart forgives and several workers count separately.
    That is fine for what it guards; it is a brake on abuse, not an audit.
    """
    count, first = _LOGIN_ATTEMPTS.get(key, (0, utcnow()))
    if utcnow() - first > timedelta(minutes=minutes):
        count, first = 0, utcnow()
    _LOGIN_ATTEMPTS[key] = (count + 1, first)
    return count < limit


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True) or {}
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
    uname = (data.get('username') or '').strip().lower()[:80]
    # Both the address and the account are throttled. Throttling only the
    # address leaves one account open to a spread-out guessing attack, and
    # throttling only the account lets one machine work through the user list.
    for key in (ip, 'u:' + uname):
        if _login_blocked(key):
            return jsonify({'error': 'Too many failed attempts. '
                                     'Try again in a few minutes.'}), 429
    user = User.query.filter_by(username=data.get('username'), is_active=True).first()
    if user and user.check_password(data.get('password', '')):
        _LOGIN_ATTEMPTS.pop(ip, None)
        _LOGIN_ATTEMPTS.pop('u:' + uname, None)
        # A fresh session on sign-in, so a token planted before login cannot be
        # reused afterwards.
        session.clear()
        session.permanent = True
        session['user_id'] = user.id
        # A stored value that is not one of the two supported codes - an older
        # row seeded before English became the default, a blank, or a
        # hand-edited database - is corrected here rather than leaving the
        # interface in a language nobody chose.
        if user.language not in ('en', 'gu'):
            user.language = DEFAULT_LANGUAGE
        session['lang'] = user.language
        session['perms'] = effective_permissions(user)
        audit('login', 'user', user, new={}, label=user.full_name or user.username)
        db.session.commit()
        session['user_role'] = user.role
        session['user_center_id'] = user.center_id
        return jsonify({'success': True, 'user': user.to_dict()})
    _login_failed(ip)
    _login_failed('u:' + uname)
    # Failed attempts are recorded too - that is the point of an audit trail.
    try:
        db.session.add(AuditLog(username=(data.get('username') or '')[:80],
                                action='login_failed', entity_type='user',
                                ip=ip, entity_label=(data.get('username') or '')[:300]))
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify({'error': 'Invalid username or password'}), 401

@app.route('/api/me')
@login_required
def api_me():
    return jsonify(User.query.get(session['user_id']).to_dict())

@app.route('/api/change-password', methods=['POST'])
@app.route('/api/password/change', methods=['POST'])
@login_required
def api_change_password():
    """Change your own password.

    Both URLs land here. The older one accepted anything and never cleared the
    must-change flag, so a user forced to change would have been asked again on
    the next sign-in.

    The current password is required even when a change is being forced -
    otherwise somebody who found an unattended session could take the account
    over without ever knowing it.
    """
    d = request.get_json(silent=True) or {}
    u = User.query.get(session['user_id'])
    current = d.get('current') or d.get('old_password') or ''
    new = d.get('new') or d.get('new_password') or ''

    if not u.check_password(current):
        return jsonify({'error': 'That is not your current password'}), 400
    problem = password_problem(new, u.username)
    if problem:
        return jsonify({'error': problem}), 400
    if u.check_password(new):
        return jsonify({'error': 'That is the password you already have. '
                                 'Choose a different one.'}), 400

    u.set_password(new)
    u.must_change_password = False
    u.password_changed_at = utcnow()
    db.session.commit()
    audit('update', 'user', u, label=u.full_name, new={'password': 'changed'})
    db.session.commit()
    return jsonify({'success': True, 'message': 'Password changed'})

@app.route('/api/password/status')
@login_required
def api_password_status():
    u = User.query.get(session['user_id'])
    return jsonify({'must_change': bool(u.must_change_password)})


def password_problem(pw, username=''):
    """Why this password will not do, or None.

    Deliberately modest rules. A long list of requirements pushes people
    towards writing it on the desk, which is worse than a short password.
    """
    pw = pw or ''
    if len(pw) < 8:
        return 'Use at least 8 characters'
    if pw.lower() in ('password', 'admin@123', 'sant@123', '12345678',
                      'password1', 'smvs@123'):
        return 'That password is too easy to guess'
    if username and pw.lower() == (username or '').lower():
        return 'Your password cannot be your username'
    if pw.isdigit():
        return 'Use something other than digits alone'
    return None


@app.route('/api/set-language', methods=['POST'])
@login_required
def api_set_language():
    data = request.get_json()
    lang = data.get('language', DEFAULT_LANGUAGE)
    if lang not in ('en', 'gu'): lang = 'en'
    user = User.query.get(session['user_id'])
    user.language = lang
    db.session.commit()
    session['lang'] = lang
    return jsonify({'success': True, 'language': lang})


# ======================== API: DASHBOARD ========================
@app.route('/api/dashboard')
@perm_required('page.dashboard')
def api_dashboard():
    year = get_current_year()
    cf = get_user_center_filter()
    qc = Center.query.filter_by(is_active=True)
    if cf: qc = qc.filter_by(id=cf)
    total_centers = qc.count()
    qm = Member.query.filter_by(is_active=True)
    if cf: qm = qm.filter_by(center_id=cf)
    total_members = qm.count()
    qs = Sankalp.query.filter_by(pujan_year=year)
    if cf: qs = qs.filter_by(center_id=cf)
    total_sankalps = qs.count()
    # SUM in the database. Loading every pledge to add it up in Python meant
    # the dashboard got slower with every pledge entered.
    amt_q = db.session.query(db.func.coalesce(db.func.sum(Sankalp.amount), 0))\
        .filter(Sankalp.pujan_year == year)
    if cf:
        amt_q = amt_q.filter(Sankalp.center_id == cf)
    total_sankalp_amount = float(amt_q.scalar() or 0)

    qcoll = Collection.query.join(Sankalp).filter(Sankalp.pujan_year == year)
    if cf: qcoll = qcoll.filter(Collection.center_id == cf)
    coll_q = db.session.query(db.func.coalesce(db.func.sum(Collection.amount), 0))\
        .join(Sankalp, Collection.sankalp_id == Sankalp.id)\
        .filter(Sankalp.pujan_year == year)
    if cf:
        coll_q = coll_q.filter(Collection.center_id == cf)
    total_collected = float(coll_q.scalar() or 0)
    total_pending = total_sankalp_amount - total_collected
    qp = Pass.query.filter_by(pujan_year=year)
    if cf: qp = qp.filter_by(center_id=cf)
    total_passes = qp.count()
    used_passes = qp.filter_by(is_used=True).count()
    qa = Attendance.query.filter_by(pujan_year=year)
    if cf: qa = qa.filter_by(center_id=cf)
    total_attendance = qa.count()
    # A centre user sees their own centre only. The breakdown table was
    # looping over EVERY active centre and the recent list was unfiltered, so
    # a centre user could read other centres' figures.
    center_stats = []
    if not cf:
        # Three grouped queries covering every centre, in place of one query
        # per centre plus one per pledge to reach its collections.
        centers = Center.query.filter_by(is_active=True).order_by(Center.name).all()
        amt_by = dict(
            db.session.query(Sankalp.center_id,
                             db.func.coalesce(db.func.sum(Sankalp.amount), 0))
            .filter(Sankalp.pujan_year == year)
            .group_by(Sankalp.center_id).all())
        n_by = dict(
            db.session.query(Sankalp.center_id, db.func.count(Sankalp.id))
            .filter(Sankalp.pujan_year == year)
            .group_by(Sankalp.center_id).all())
        coll_by = dict(
            db.session.query(Collection.center_id,
                             db.func.coalesce(db.func.sum(Collection.amount), 0))
            .join(Sankalp, Collection.sankalp_id == Sankalp.id)
            .filter(Sankalp.pujan_year == year)
            .group_by(Collection.center_id).all())
        for c in centers:
            amt = float(amt_by.get(c.id, 0) or 0)
            coll = float(coll_by.get(c.id, 0) or 0)
            center_stats.append({'center_name': c.label,
                                 'sankalps': n_by.get(c.id, 0),
                                 'amount': amt, 'collected': coll,
                                 'pending': amt - coll})
    qrs = Sankalp.query.filter_by(pujan_year=year)
    if cf:
        qrs = qrs.filter_by(center_id=cf)
    recent_sankalps = qrs.order_by(Sankalp.created_at.desc()).limit(10).all()
    recent_collections = qcoll.order_by(Collection.created_at.desc()).limit(10).all()
    return jsonify({'year': year, 'stats': {'total_centers': total_centers, 'total_members': total_members, 'total_sankalps': total_sankalps, 'total_sankalp_amount': total_sankalp_amount, 'total_collected': total_collected, 'total_pending': total_pending, 'total_passes': total_passes, 'used_passes': used_passes, 'total_attendance': total_attendance}, 'show_center_breakdown': not cf, 'center_stats': center_stats, 'recent_sankalps': [s.to_dict() for s in recent_sankalps], 'recent_collections': [c.to_dict() for c in recent_collections]})


# ======================== API: CENTERS ========================
def center_counts(center_ids=None):
    """Member and pledge counts for every centre, in two queries rather than
    two per centre.

    At 500 members the difference is invisible. At 5,000 across thirty centres
    it is sixty round trips against two, on a page that loads constantly.
    """
    mq = (db.session.query(Member.center_id, db.func.count(Member.id))
          .filter(Member.is_active.is_(True)))
    sq = db.session.query(Sankalp.center_id, db.func.count(Sankalp.id))
    if center_ids:
        mq = mq.filter(Member.center_id.in_(center_ids))
        sq = sq.filter(Sankalp.center_id.in_(center_ids))
    members = dict(mq.group_by(Member.center_id).all())
    sankalps = dict(sq.group_by(Sankalp.center_id).all())
    return {cid: {'members': members.get(cid, 0), 'sankalps': sankalps.get(cid, 0)}
            for cid in set(members) | set(sankalps) | set(center_ids or [])}


@app.route('/api/centers', methods=['GET'])
@login_required
def api_get_centers():
    cf = get_user_center_filter()
    q = Center.query
    if cf: q = q.filter_by(id=cf)
    rows = q.order_by(Center.name).all()
    counts = center_counts([c.id for c in rows])
    return jsonify([c.to_dict(counts.get(c.id)) for c in rows])

@app.route('/api/centers/<int:cid>', methods=['GET'])
@login_required
def api_get_center(cid):
    """BUGFIX: this route did not exist, so the Edit button on the centre list
    hit a 405 and the UI showed 'Something went wrong'. Members and users had
    the identical gap."""
    return jsonify(Center.query.get_or_404(cid).to_dict())


def _center_names(data, existing=None):
    """English is the required name. Gujarati is optional and falls back to
    the English one, so nobody is blocked on typing Gujarati."""
    name_en = (data.get('name_en') or '').strip()
    name_gu = (data.get('name') or '').strip()
    if not name_en and not name_gu:
        if existing:
            return existing.name, existing.name_en
        return None, None
    return (name_gu or name_en), (name_en or name_gu)


@app.route('/api/centers', methods=['POST'])
@perm_required('center.manage')
def api_create_center():
    data = request.get_json(silent=True) or {}
    name_gu, name_en = _center_names(data)
    if not name_en:
        return jsonify({'error': 'Center name is required'}), 400
    c = Center(name=name_gu, name_en=name_en,
               city=(data.get('city') or '').strip(),
               phone=(data.get('phone') or '').strip(),
               email=(data.get('email') or '').strip(),
               address=(data.get('address') or '').strip(),
               is_active=bool(data.get('is_active', True)))
    db.session.add(c); db.session.commit()
    return jsonify({'success': True, 'center': c.to_dict()})


@app.route('/api/centers/<int:cid>', methods=['PUT'])
@perm_required('center.manage')
def api_update_center(cid):
    data = request.get_json(silent=True) or {}
    c = Center.query.get_or_404(cid)
    # A bare {"is_active": false} from the list toggle must not wipe the names.
    if 'name' in data or 'name_en' in data:
        c.name, c.name_en = _center_names(data, existing=c)
    for field in ('city', 'phone', 'email', 'address'):
        if field in data:
            setattr(c, field, (data.get(field) or '').strip())
    if 'is_active' in data:
        c.is_active = bool(data['is_active'])
    db.session.commit()
    return jsonify({'success': True, 'center': c.to_dict()})


@app.route('/api/centers/<int:cid>', methods=['DELETE'])
@perm_required('center.manage')
def api_delete_center(cid):
    """Permanent delete, allowed only for a centre that holds nothing. To take
    a centre out of use without losing its history, flip is_active instead
    (the toggle in the Status column does exactly that)."""
    c = Center.query.get_or_404(cid)
    blockers = []
    if c.members.count():
        blockers.append('members')
    if c.sankalps.count():
        blockers.append('sankalps')
    if Pass.query.filter_by(center_id=cid).count():
        blockers.append('passes')
    if User.query.filter_by(center_id=cid).count():
        blockers.append('users')
    if blockers:
        return jsonify({'error': 'This center still has ' + ', '.join(blockers) +
                                 '. Use the Active toggle to take it out of use instead.'}), 400
    db.session.delete(c); db.session.commit()
    return jsonify({'success': True})


# ======================== API: MEMBERS ========================
MEMBER_TYPES = ('individual', 'firm', 'company')


def _sync_entities(member, entities_payload):
    """Bring the member's firms and companies in line with what was submitted.

    Updates in place rather than replacing wholesale. The old version cleared
    the collection and rebuilt it, which fails the moment a pledge points at
    one of those rows:

        ForeignKeyViolation: update or delete on table "entities" violates
        foreign key constraint "sankalps_entity_id_fkey"

    A firm carrying a pledge is kept and updated; removing it from the form is
    refused with a message naming the firm, the same way deleting a sankalp
    with money against it is refused.
    """
    if entities_payload is None:
        return

    existing = {e.id: e for e in member.entities}
    by_key = {(e.entity_type, (e.name or '').strip().lower()): e for e in member.entities}
    kept = set()

    for raw in entities_payload:
        etype = raw.get('entity_type')
        name = (raw.get('name') or '').strip()
        if etype not in ('firm', 'company') or not name:
            continue                      # silently drop blank repeater rows

        # Match on the id the form sent back, falling back to type+name so a
        # row that lost its id is still recognised rather than duplicated.
        ent = None
        rid = raw.get('id')
        if rid and int(rid) in existing:
            ent = existing[int(rid)]
        if ent is None:
            ent = by_key.get((etype, name.lower()))
        if ent is None:
            ent = Entity(entity_type=etype, name=name)
            member.entities.append(ent)

        ent.entity_type = etype
        ent.name = name
        ent.address = (raw.get('address') or '').strip()
        ent.email = (raw.get('email') or '').strip()
        ent.contact = (raw.get('contact') or '').strip()
        ent.gst_number = (raw.get('gst_number') or '').strip()
        db.session.flush()
        kept.add(ent.id)

        _sync_people(ent, raw.get('people') or [])

    # Anything the form no longer lists.
    for ent in list(member.entities):
        if ent.id in kept:
            continue
        n = Sankalp.query.filter_by(entity_id=ent.id).count()
        if n:
            raise ValueError(f'"{ent.name}" has {n} sankalp(s) against it. '
                             f'Delete those first, then remove the {ent.entity_type}.')
        member.entities.remove(ent)
    db.session.flush()


def _sync_people(ent, people_payload):
    """The partners or directors on one firm, updated in place for the same
    reason - a pledge can be taken in a partner's name."""
    existing = {p.id: p for p in ent.people}
    by_name = {(p.name or '').strip().lower(): p for p in ent.people}
    kept = set()

    for pr in people_payload:
        pname = (pr.get('name') or '').strip()
        if not pname:
            continue
        # A Member ID is only needed when this partner belongs to another
        # centre, because that is what decides who collects their pledge.
        code = (pr.get('member_code') or '').strip().upper()
        if not code and pr.get('center_id'):
            raise ValueError(f'A {ent.role_label.lower()} in another center needs '
                             f'a Member ID - "{pname}"')

        person = None
        pid = pr.get('id')
        if pid and int(pid) in existing:
            person = existing[int(pid)]
        if person is None:
            person = by_name.get(pname.lower())
        if person is None:
            person = EntityPerson(name=pname)
            ent.people.append(person)

        person.name = pname
        person.member_code = code
        # Fixed by the entity type, never typed in.
        person.designation = 'Director' if ent.entity_type == 'company' else 'Partner'
        person.mobile = (pr.get('mobile') or '').strip()
        person.email = (pr.get('email') or '').strip()
        person.center_id = pr.get('center_id') or None
        db.session.flush()
        kept.add(person.id)

    for person in list(ent.people):
        if person.id in kept:
            continue
        n = Sankalp.query.filter_by(person_id=person.id).count()
        if n:
            raise ValueError(f'{person.name} has {n} sankalp(s) against them. '
                             f'Delete those first.')
        ent.people.remove(person)


def _center_prefix(center):
    """SUR, VAD, HO ... derived from the English centre name."""
    src = (center.name_en or center.name or 'M') if center else 'M'
    words = [w for w in ''.join(ch if ch.isalnum() or ch == ' ' else ' '
                                for ch in src).split() if w.lower() != 'center']
    if not words:
        return 'MEM'
    if len(words) == 1:
        return words[0][:3].upper()
    return ''.join(w[0] for w in words[:3]).upper()


def _sankalp_search_clause(term, year):
    """One search term, matched across everything a sankalp row shows -
    including the seat number, which lives on the check-in."""
    like = f'%{term}%'
    mem_sub = db.session.query(Member.id).filter(db.or_(
        Member.name.ilike(like), Member.member_code.ilike(like),
        Member.mobile.ilike(like)))
    ctr_sub = db.session.query(Center.id).filter(db.or_(
        Center.name.ilike(like), Center.name_en.ilike(like), Center.city.ilike(like)))
    ent_sub = db.session.query(Entity.id).filter(Entity.name.ilike(like))
    per_sub = db.session.query(EntityPerson.id).filter(db.or_(
        EntityPerson.name.ilike(like), EntityPerson.member_code.ilike(like)))
    conds = [Sankalp.sankalp_name.ilike(like), Sankalp.member_id.in_(mem_sub),
             Sankalp.center_id.in_(ctr_sub), Sankalp.owner_center_id.in_(ctr_sub),
             Sankalp.entity_id.in_(ent_sub), Sankalp.person_id.in_(per_sub)]
    digits = term.replace(',', '').strip()
    if digits.isdigit():
        # A bare number is either an amount or a seat number.
        try:
            conds.append(Sankalp.amount == Decimal(digits))
        except (InvalidOperation, ValueError):
            pass
        seat_sub = db.session.query(Attendance.member_id).filter(
            Attendance.pujan_year == year, Attendance.seat_no == int(digits))
        conds.append(Sankalp.member_id.in_(seat_sub))
    return db.or_(*conds)


def _money(v):
    """A number from whatever the form sent, or zero."""
    try:
        return Decimal(str(v).replace(',', '').strip() or '0')
    except (InvalidOperation, ValueError, AttributeError):
        return Decimal('0')


def next_member_code(center_id):
    """Suggest the next free code for a centre, e.g. SUR-0007."""
    center = db.session.get(Center, center_id) if center_id else None
    prefix = _center_prefix(center)
    n = 0
    for (code,) in db.session.query(Member.member_code).filter(
            Member.member_code.like(f'{prefix}-%')).all():
        try:
            n = max(n, int(str(code).rsplit('-', 1)[-1]))
        except (ValueError, TypeError):
            continue
    while True:
        n += 1
        candidate = f'{prefix}-{n:04d}'
        if not Member.query.filter_by(member_code=candidate).first():
            return candidate


@app.route('/api/members/next-code')
@login_required
def api_next_member_code():
    cid = request.args.get('center_id', type=int) or session.get('user_center_id')
    return jsonify({'member_code': next_member_code(cid)})


def _validate_member_code(code, member_id=None):
    code = (code or '').strip().upper()
    if not code:
        return None, 'Member ID is required'
    if len(code) > 50:
        return None, 'Member ID is too long'
    clash = Member.query.filter(Member.member_code == code)
    if member_id:
        clash = clash.filter(Member.id != member_id)
    if clash.first():
        return None, f'Member ID "{code}" is already in use'
    return code, None


@app.route('/api/members', methods=['GET'])
@perm_required('page.members', 'page.sankalps', 'page.passes', 'page.collections')
def api_get_members():
    cf = get_user_center_filter()
    year = request.args.get('year', get_current_year(), type=int)
    q = Member.query.filter_by(is_active=True)
    if cf:
        q = q.filter_by(center_id=cf)
    cid = request.args.get('center_id', type=int)
    if cid:
        q = q.filter_by(center_id=cid)
    mtype = request.args.get('member_type')
    if mtype:
        q = q.filter_by(member_type=mtype)
    search = (request.args.get('search') or '').strip()
    if search:
        like = f'%{search}%'
        # One box, everything: member name and ID, mobile, PAN, email, address,
        # firm/company names, partner names, centre name, and the pledge name
        # or amount for the year being viewed.
        ent_sub = db.session.query(Entity.member_id).outerjoin(EntityPerson).filter(
            db.or_(Entity.name.ilike(like), EntityPerson.name.ilike(like),
                   EntityPerson.member_code.ilike(like)))
        center_sub = db.session.query(Center.id).filter(
            db.or_(Center.name.ilike(like), Center.name_en.ilike(like),
                   Center.city.ilike(like)))
        sank_sub = db.session.query(Sankalp.member_id).filter(
            Sankalp.pujan_year == year, Sankalp.sankalp_name.ilike(like))
        conds = [
            Member.name.ilike(like), Member.mobile.ilike(like),
            Member.member_code.ilike(like), Member.pan_number.ilike(like),
            Member.email.ilike(like), Member.address.ilike(like),
            Member.id.in_(ent_sub), Member.center_id.in_(center_sub),
            Member.id.in_(sank_sub),
        ]
        # A bare number also matches a pledge amount, so "25000" finds them.
        digits = search.replace(',', '').replace('\u20b9', '').strip()
        try:
            amt = Decimal(digits)
            conds.append(Member.id.in_(db.session.query(Sankalp.member_id).filter(
                Sankalp.pujan_year == year, Sankalp.amount == amt)))
        except (InvalidOperation, ValueError):
            pass
        q = q.filter(db.or_(*conds))
    rows = [m.to_dict(include_sankalp=True, year=year)
            for m in q.order_by(Member.name).all()]
    # Whoever enters members need not see what anyone pledged. Stripped on the
    # server, so it is not merely hidden in the page.
    return jsonify([strip_money(d) for d in rows])


@app.route('/api/members/<int:mid>', methods=['GET'])
@perm_required('page.members', 'page.sankalps')
def api_get_member(mid):
    """BUGFIX: the Edit button called this and got a 405 - the route was never
    registered, only PUT and DELETE were."""
    m = Member.query.get_or_404(mid)
    cf = get_user_center_filter()
    if cf and m.center_id != cf:
        return jsonify({'error': 'Permission denied'}), 403
    year = request.args.get('year', get_current_year(), type=int)
    return jsonify(m.to_dict(include_sankalp=True, year=year))


@app.route('/api/members/quick')
@perm_required('member.fast_entry', 'sankalp.fast_entry', 'page.members')
def api_member_quick():
    """Resolve what the fast-entry grid typed - a Member ID, or part of a name.

    Returns the member plus every thing a pledge could be raised against, as a
    single flat list, so the grid needs one dropdown instead of three.
    """
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify({'found': False})
    cf = get_user_center_filter()
    base = Member.query.filter_by(is_active=True)
    if cf:
        base = base.filter_by(center_id=cf)
    m = base.filter(db.func.upper(Member.member_code) == q.upper()).first()
    if not m:
        hits = base.filter(Member.name.ilike(f'%{q}%')).limit(6).all()
        if len(hits) != 1:
            return jsonify({'found': False, 'ambiguous': len(hits) > 1,
                            'matches': [{'id': x.id, 'member_code': x.member_code or '',
                                         'name': x.name} for x in hits]})
        m = hits[0]
    targets = [{'key': 'individual', 'label': m.name + ' (' + 'individual' + ')',
                'sankalp_type': 'individual', 'entity_id': None}]
    for e in m.entities:
        targets.append({
            'key': f'{e.entity_type}:{e.id}',
            'label': e.name + ' (' + e.entity_type + ')',
            'sankalp_type': e.entity_type, 'entity_id': e.id})
    return jsonify({'found': True, 'id': m.id, 'member_code': m.member_code or '',
                    'name': m.name, 'center_id': m.center_id,
                    'center_name': m.center.label if m.center else '',
                    'attend_at_ho': bool(m.attend_at_ho), 'targets': targets,
                    # Everything the member fast-entry grid needs to fill a row
                    # in from an existing record rather than retyping it.
                    'mobile': m.mobile or '', 'member_type': m.member_type,
                    'businesses': [{'id': e.id, 'entity_type': e.entity_type, 'name': e.name}
                                   for e in m.entities],
                    'last_year': _last_pledge_year(m)})


def _last_pledge_year(m):
    """The member's most recent pledge, so the grid can show what they gave
    last time without a second request."""
    sk = Sankalp.query.filter_by(member_id=m.id).order_by(
        Sankalp.pujan_year.desc()).first()
    if not sk:
        return None
    return {'year': sk.pujan_year, 'amount': float(sk.amount),
            'sankalp_name': sk.sankalp_name}


@app.route('/api/members/recent')
@perm_required('member.fast_entry', 'page.members')
def api_members_recent():
    """Members this user added or updated recently.

    Read back from the audit trail rather than held in the browser, so the list
    survives closing the dialog, a page refresh, or moving to another device.
    Each person sees only their own work, which is what you want when several
    volunteers are entering at once.
    """
    uid = session['user_id']
    year = request.args.get('year', type=int) or get_current_year()
    q = (AuditLog.query
         .filter(AuditLog.user_id == uid,
                 AuditLog.entity_type == 'member',
                 AuditLog.action.in_(['create', 'update']),
                 AuditLog.pujan_year == year))
    days = request.args.get('days', type=int)
    if days:
        q = q.filter(AuditLog.created_at >= utcnow() - timedelta(days=min(365, max(1, days))))
    logs = q.order_by(AuditLog.created_at.desc()).limit(500).all()
    rows, seen = [], set()
    for lg in logs:
        if lg.entity_id in seen:
            continue
        seen.add(lg.entity_id)
        m = db.session.get(Member, lg.entity_id) if lg.entity_id else None
        # A member deleted after being entered should leave the list rather
        # than sit there as a row that no longer exists.
        if not m or not m.is_active:
            continue
        rows.append({
            'id': m.id, 'member_code': m.member_code or '', 'name': m.name,
            'mobile': m.mobile or '',
            'center_name': m.center.label if m.center else '',
            'businesses': [e.name for e in m.entities],
            'existing': lg.action == 'update',
            'when': to_ist(lg.created_at, '%d-%m-%Y %H:%M'),
            # Whatever event-day registration produced, so the list still shows
            # the pass and seat after the dialog is reopened.
            'pass_number': (lambda p: p.pass_number if p else '')(
                Pass.query.filter_by(member_id=m.id, pujan_year=year).first()),
            'seat_label': (lambda a: a.seat_label if a else '')(
                Attendance.query.filter_by(member_id=m.id, pujan_year=year).first()),
        })
    rows.reverse()                       # oldest first, so numbering reads 1..n
    return jsonify({'rows': rows, 'total': len(rows), 'days': days})


@app.route('/api/members/bulk', methods=['POST'])
@perm_required('member.fast_entry')
def api_members_bulk():
    """Retries once or twice if two people generate the same auto Member ID at
    the same moment. The unique index catches it; this recovers from it."""
    for attempt in range(6):
        try:
            return _members_bulk_once()
        except IntegrityError:
            db.session.rollback()
            # A pause, so two desks that just collided do not immediately
            # recompute the same next member code.
            time.sleep(random.uniform(0.01, 0.06) * (attempt + 1))
            if attempt == 5:
                return jsonify({'ok': False, 'saved': 0, 'errors': [{
                    'row': 0,
                    'message': 'Someone else saved at the same moment. '
                               'Press Save all again.'}]}), 409


def get_pass_config(year):
    """The year's pass numbering, creating it if this is the first time.

    Two people opening the Passes screen at the same moment would both try to
    create the row, and the second insert would fail. The retry turns that into
    "read the one the other just made".
    """
    config = PassConfig.query.filter_by(pujan_year=year).first()
    if config:
        return config
    config = PassConfig(pujan_year=year)
    db.session.add(config)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        config = PassConfig.query.filter_by(pujan_year=year).first()
    return config


def next_pass_number(pass_type, year):
    """The next pass number for this type and year.

    Pulled out of the bulk generator so fast-entry registration on the day uses
    exactly the same sequence - two implementations would sooner or later hand
    out the same number twice.
    """
    config = get_pass_config(year)
    prefix = config.prefix_for(pass_type)
    next_num = config.start_for(pass_type)
    # Sorting on the numeric suffix rather than the id keeps the sequence right
    # even when rows were inserted out of order.
    for p in Pass.query.filter_by(pujan_year=year, pass_type=pass_type).all():
        try:
            v = int(p.pass_number.replace(prefix, ''))
        except ValueError:
            continue
        if v >= next_num:
            next_num = v + 1
    return f'{prefix}{str(next_num).zfill(4)}'


def claim_seat(member, p, year):
    """The next free seat for this person, retried until it sticks.

    Several desks reading "the highest seat so far" at the same instant will
    all compute the same next number. The unique index rejects the duplicate,
    and this retries with a short random pause so the two do not simply collide
    again on the next attempt. Ten attempts with jitter is far more than a
    handful of desks needs, and a seat is not something to give up on.
    """
    last = None
    for attempt in range(10):
        seat = (db.session.query(db.func.max(Attendance.seat_no))
                .filter_by(pujan_year=year).scalar() or 0) + 1
        att = Attendance(pass_id=p.id, member_id=member.id,
                         center_id=member.center_id, pujan_year=year, seat_no=seat)
        db.session.add(att)
        try:
            db.session.commit()
            return att
        except IntegrityError as e:
            last = e
            db.session.rollback()
            # Someone else may have checked this very person in meanwhile.
            existing = Attendance.query.filter_by(member_id=member.id,
                                                  pujan_year=year).first()
            if existing:
                return existing
            time.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
    raise last


def event_register(member_id, year, pass_type, user):
    """Pass, check-in and seat for one member, in that order.

    This is what "Today is Event Date" does in fast entry: someone walks up on
    the day, is entered, and should immediately have a pass number, be marked
    present and hold a seat - without three more screens.

    Returns what to show back on the row. Safe to call twice: an existing pass
    or check-in is reused rather than duplicated.
    """
    m = Member.query.get(member_id)
    if not m:
        return {}

    # Where they are standing decides where they are counted. A Head Office
    # volunteer is registering someone at the HO desk, so that person attends
    # at HO; a centre user is registering at their own centre.
    at_ho = not is_center_scoped(user)
    if bool(m.attend_at_ho) != at_ho or m.event_registered_year != year:
        m.attend_at_ho = at_ho
        # This is what makes them count as New rather than Expecting.
        m.event_registered_year = year
        db.session.commit()

    # 1. The pass. Reuse one if this member already has it for the year.
    p = Pass.query.filter_by(member_id=m.id, pujan_year=year).first()
    if not p:
        # Two desks registering walk-ins at the same moment will both read the
        # same "next" number. The unique index stops the duplicate; this loop
        # is what turns that into a second try rather than a person left
        # standing at the door with no pass.
        for attempt in range(6):
            p = Pass(pass_number=next_pass_number(pass_type, year),
                     pass_type=pass_type, member_id=m.id, center_id=m.center_id,
                     pujan_year=year, created_by=user.id)
            db.session.add(p)
            try:
                db.session.commit()
                break
            except IntegrityError:
                db.session.rollback()
                p = Pass.query.filter_by(member_id=m.id, pujan_year=year).first()
                if p:
                    break            # another thread issued one for this member
                if attempt == 5:
                    raise
        audit('create', 'pass', p, label=p.pass_number, year=year)
        db.session.commit()

    # 2. The check-in, and 3. the seat that comes with it.
    att = Attendance.query.filter_by(member_id=m.id, pujan_year=year).first()
    if not att:
        att = claim_seat(m, p, year)
        p.is_used = True
        db.session.commit()
        audit('create', 'attendance', att, label=m.display_name, year=year,
              new={'seat_no': att.seat_no, 'pass_number': p.pass_number,
                   'via': 'fast entry on the event date'})
        db.session.commit()
    else:
        p.is_used = True
        db.session.commit()

    # 4. A pledge line, at zero, so they appear on the Sankalp list.
    #
    # Someone registered at the door has usually not pledged yet, but leaving
    # them off the list means nobody can find them there to enter it later -
    # and the centre totals would silently miss them. One row per firm, or one
    # for the person if they have none, matching how the list is laid out.
    made = ensure_zero_sankalps(m, year, user)

    return {'pass_number': p.pass_number, 'pass_type': p.pass_type,
            'seat_no': att.seat_no if att else None,
            'seat_label': att.seat_label if att else '',
            'sankalps_added': made,
            'registered': True}


def ensure_zero_sankalps(m, year, user):
    """A zero pledge for anything of this member's that has none yet.

    Zero rather than nothing, so the member is on the Sankalp list and the
    amount can be filled in when they decide - by editing the row, or in bulk
    edit. Never touches a row that already has an amount.
    """
    existing = Sankalp.query.filter_by(member_id=m.id, pujan_year=year).all()
    made = 0

    if m.entities:
        have = {sk.entity_id for sk in existing if sk.entity_id}
        for e in m.entities:
            if e.id in have:
                continue
            sk = Sankalp(member_id=m.id, center_id=m.center_id,
                         owner_center_id=m.center_id, entity_id=e.id,
                         pujan_year=year, sankalp_type=e.entity_type,
                         sankalp_name=e.name, amount=0, created_by=user.id)
            db.session.add(sk)
            made += 1
    elif not existing:
        sk = Sankalp(member_id=m.id, center_id=m.center_id,
                     owner_center_id=m.center_id, pujan_year=year,
                     sankalp_type='individual', sankalp_name=m.name,
                     amount=0, created_by=user.id)
        db.session.add(sk)
        made += 1

    if made:
        db.session.commit()
        for sk in Sankalp.query.filter_by(member_id=m.id, pujan_year=year).all():
            if sk not in existing:
                audit('create', 'sankalp', sk, label=sk.sankalp_name, year=year,
                      new={'amount': 0, 'via': 'registered at the door'})
        db.session.commit()
    return made


def _members_bulk_once():
    """Fast entry: many members in one go, minimal fields.

    Rows carrying the SAME Member ID are one member with several businesses -
    that is what the + button produces. A Member ID that already exists updates
    that member and appends any new firm/company rather than being refused.
    """
    data = request.get_json(silent=True) or {}
    rows = data.get('rows') or []
    year = int(data.get('pujan_year') or get_current_year())
    user = User.query.get(session['user_id'])
    cf = get_user_center_filter()
    errors, saved = [], []

    # Group by Member ID; blank IDs are each their own new member.
    groups, order = {}, []
    for i, r in enumerate(rows):
        line = r.get('_row', i + 1)
        if not (r.get('name') or '').strip() and not (r.get('business') or '').strip():
            continue                                   # blank row: skip quietly
        # Rows the + button produced share a client-side group id, so several
        # businesses for one person stay one member even before an ID exists.
        # Falling back to the row index made each row its own member.
        key = ((r.get('member_code') or '').strip().upper()
               or ('grp:' + str(r['group']) if r.get('group') else f'__new{i}'))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((line, r))

    for key in order:
        entries = groups[key]
        first_line, first = entries[0]
        is_new_key = key.startswith('__new') or key.startswith('grp:')

        names = {(r.get('name') or '').strip() for _, r in entries if (r.get('name') or '').strip()}
        cid = cf or first.get('center_id') or user.center_id
        try:
            cid = int(cid)
        except (TypeError, ValueError):
            errors.append({'row': first_line, 'message': 'Center is required'}); continue

        existing = (None if (is_new_key or key.startswith('grp:'))
                    else Member.query.filter_by(member_code=key).first())
        if not names and not existing:
            errors.append({'row': first_line, 'message': 'Name is required'}); continue
        if len(names) > 1:
            errors.append({'row': first_line, 'message':
                f'Member ID "{key}" is used for different names: ' + ' / '.join(sorted(names))}); continue
        name = names.pop() if names else existing.name

        mtype = (first.get('member_type') or 'individual').strip().lower()
        if mtype not in MEMBER_TYPES:
            errors.append({'row': first_line, 'message': f'Unknown type "{mtype}"'}); continue

        businesses = []
        for line, r in entries:
            b = (r.get('business') or '').strip()
            bt = (r.get('member_type') or mtype).strip().lower()
            if bt in ('firm', 'company') and not b:
                errors.append({'row': line, 'message': f'{bt.title()} name is required for {name}'})
                break
            if b:
                businesses.append((bt if bt in ('firm', 'company') else mtype, b))
        else:
            if existing:
                if cf and existing.center_id != cf:
                    errors.append({'row': first_line,
                                   'message': f'{name} belongs to another center'}); continue
                before = snapshot(existing, 'member')
                existing.name = name
                if (first.get('mobile') or '').strip():
                    existing.mobile = first['mobile'].strip()
                if mtype != 'individual':
                    existing.member_type = mtype
                for bt, b in businesses:
                    if not any(e.entity_type == bt and e.name.lower() == b.lower()
                               for e in existing.entities):
                        existing.entities.append(Entity(entity_type=bt, name=b))
                db.session.flush()
                audit('update', 'member', existing, old=before,
                      new=snapshot(existing, 'member'), label=name, year=year)
                saved.append({'row': first_line, 'id': existing.id, 'name': name,
                              'member_code': existing.member_code or '',
                              'center_name': existing.center.label if existing.center else '',
                              'mobile': existing.mobile or '',
                              'businesses': [b for _, b in businesses],
                              'existing': True})
                continue

            code = key if not is_new_key else next_member_code(cid)
            code, err = _validate_member_code(code)
            if err:
                errors.append({'row': first_line, 'message': err}); continue
            m = Member(center_id=cid, member_code=code, member_type=mtype, name=name,
                       mobile=(first.get('mobile') or '').strip())
            db.session.add(m)
            for bt, b in businesses:
                m.entities.append(Entity(entity_type=bt, name=b))
            db.session.flush()
            audit('create', 'member', m, label=name, year=year)
            saved.append({'row': first_line, 'id': m.id, 'member_code': code, 'name': name,
                          'center_name': m.center.label if m.center else '',
                          'mobile': m.mobile or '',
                          'businesses': [b for _, b in businesses],
                          'existing': False})

    if errors:
        db.session.rollback()                # all or nothing, like the CSV import
        return jsonify({'ok': False, 'saved': 0, 'errors': errors,
                        'would_save': len(saved)}), 400
    db.session.commit()

    # Registering on the day itself: a pass, a check-in and a seat, in that
    # order, for each person as they are entered. Done after the commit so a
    # problem here cannot lose the members themselves.
    if data.get('event_mode'):
        ptype = (data.get('pass_type') or 'general').strip().lower()
        for row in saved:
            try:
                row.update(event_register(row['id'], year, ptype, user))
            except Exception as e:
                db.session.rollback()
                row['event_error'] = f'{e.__class__.__name__}: {e}'
                app.logger.warning('Event registration failed for %s: %s', row['id'], e)

    return jsonify({'ok': True, 'saved': len(saved), 'rows': saved,
                    'created': sum(0 if x['existing'] else 1 for x in saved),
                    'updated': sum(1 if x['existing'] else 0 for x in saved),
                    'event_mode': bool(data.get('event_mode')),
                    'errors': []})


@app.route('/api/members', methods=['POST'])
@perm_required('member.create')
def api_create_member():
    data = request.get_json(silent=True) or {}
    user = User.query.get(session['user_id'])
    cid = data.get('center_id')
    if is_center_scoped(user):
        cid = user.center_id
    if not cid:
        return jsonify({'error': 'Select a center'}), 400
    mtype = data.get('member_type', 'individual')
    if mtype not in MEMBER_TYPES:
        return jsonify({'error': 'Unknown member type'}), 400
    if not (data.get('name') or '').strip():
        return jsonify({'error': 'Name is required'}), 400
    code, err = _validate_member_code(data.get('member_code'))
    if err:
        return jsonify({'error': err}), 400
    m = Member(center_id=cid, member_code=code, member_type=mtype, name=data['name'].strip(),
               mobile=data.get('mobile', ''), email=data.get('email', ''),
               address=data.get('address', ''),
               dharmada_type=(data.get('dharmada_type') or '').strip(),
               dharmada_amount=_money(data.get('dharmada_amount')),
               designation=data.get('designation', ''))
    db.session.add(m)
    try:
        _sync_entities(m, data.get('entities', []))
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    if mtype in ('firm', 'company') and not m.entities:
        db.session.rollback()
        label = 'firm' if mtype == 'firm' else 'company'
        return jsonify({'error': f'Add at least one {label} with a name'}), 400
    db.session.commit()
    audit('create', 'member', m)
    db.session.commit()
    return jsonify({'success': True, 'member': m.to_dict()})


@app.route('/api/members/<int:mid>', methods=['PUT'])
@perm_required('member.edit')
def api_update_member(mid):
    data = request.get_json(silent=True) or {}
    m = Member.query.get_or_404(mid)
    before = snapshot(m, 'member')
    user = User.query.get(session['user_id'])
    if is_center_scoped(user) and m.center_id != user.center_id:
        return jsonify({'error': 'Permission denied'}), 403
    if 'member_type' in data:
        if data['member_type'] not in MEMBER_TYPES:
            return jsonify({'error': 'Unknown member type'}), 400
        m.member_type = data['member_type']
    if user.role not in ('center_sant', 'center_user') and data.get('center_id'):
        m.center_id = data['center_id']
    if 'member_code' in data:
        code, err = _validate_member_code(data.get('member_code'), member_id=m.id)
        if err:
            return jsonify({'error': err}), 400
        m.member_code = code
    if 'dharmada_type' in data:
        m.dharmada_type = (data.get('dharmada_type') or '').strip()
    if 'dharmada_amount' in data:
        m.dharmada_amount = _money(data.get('dharmada_amount'))
    for field in ('name', 'mobile', 'email', 'address', 'designation'):
        if field in data:
            setattr(m, field, (data.get(field) or '').strip())
    if 'is_active' in data:
        m.is_active = bool(data['is_active'])
    if 'entities' in data:
        try:
            _sync_entities(m, data['entities'])
        except ValueError as e:
            db.session.rollback()
            return jsonify({'error': str(e)}), 400
        if m.member_type in ('firm', 'company') and not m.entities:
            db.session.rollback()
            label = 'firm' if m.member_type == 'firm' else 'company'
            return jsonify({'error': f'Add at least one {label} with a name'}), 400
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.exception('Saving member %s failed', mid)
        return jsonify({'error': f'Could not save this member - {e.__class__.__name__}. '
                                 f'{str(e)[:160]}'}), 400
    audit('update', 'member', m, old=before, new=snapshot(m, 'member'))
    db.session.commit()
    return jsonify({'success': True, 'member': m.to_dict()})


@app.route('/api/members/<int:mid>', methods=['DELETE'])
@perm_required('member.delete')
def api_delete_member(mid):
    m = Member.query.get_or_404(mid)
    denied = guard_center(m, 'member')
    if denied:
        return denied
    before = snapshot(m, 'member')
    m.is_active = False
    db.session.commit()
    audit('delete', 'member', m, old=before, new=snapshot(m, 'member'))
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/members/<int:mid>/ho', methods=['POST'])
@perm_required('member.ho')
def api_toggle_ho(mid):
    """The centre sant ticks who travels to Head Office."""
    data = request.get_json(silent=True) or {}
    m = Member.query.get_or_404(mid)
    user = User.query.get(session['user_id'])
    cf = get_user_center_filter()
    if cf and m.center_id != cf:
        return jsonify({'error': 'That member belongs to another center'}), 403
    want = bool(data.get('attend_at_ho'))
    year = data.get('pujan_year') or get_current_year()
    if not want and m.attend_at_ho and is_center_scoped(user):
        # Unticking after HO has already recorded a pledge would strand it.
        if Sankalp.query.filter_by(member_id=m.id, pujan_year=year).filter(
                Sankalp.center_id != m.center_id).first():
            return jsonify({'error': 'Head Office has already recorded a sankalp '
                                     'for this member. Ask an admin to change it.'}), 403
    m.attend_at_ho = want
    m.ho_year = int(year) if want else None
    m.ho_marked_by = user.id if want else None
    m.ho_marked_at = utcnow() if want else None
    db.session.commit()
    return jsonify({'success': True, 'member': m.to_dict()})


@app.route('/api/members/bulk-ho', methods=['POST'])
@perm_required('member.ho')
def api_bulk_ho():
    data = request.get_json(silent=True) or {}
    user = User.query.get(session['user_id'])
    cf = get_user_center_filter()
    want = bool(data.get('attend_at_ho'))
    year = int(data.get('pujan_year') or get_current_year())
    ids = [int(i) for i in (data.get('member_ids') or [])]
    q = Member.query.filter(Member.id.in_(ids))
    if cf:
        q = q.filter_by(center_id=cf)
    n = 0
    for m in q.all():
        m.attend_at_ho = want
        m.ho_year = year if want else None
        m.ho_marked_by = user.id if want else None
        m.ho_marked_at = utcnow() if want else None
        n += 1
    db.session.commit()
    return jsonify({'success': True, 'updated': n})


@app.route('/api/ho-attendees')
@perm_required('member.ho', 'page.members')
def api_ho_attendees():
    """Head Office's combined list: everyone every centre has sent in.

    Visible to admins in full; a centre sees only the people it sent, so it can
    check its own list without reading anybody else's.
    """
    year = request.args.get('year', get_current_year(), type=int)
    cf = get_user_center_filter()
    q = Member.query.filter_by(is_active=True, attend_at_ho=True)
    if cf:
        q = q.filter_by(center_id=cf)
    rows, by_center = [], {}
    for m in q.order_by(Member.center_id, Member.name).all():
        p = Pass.query.filter_by(member_id=m.id, pujan_year=year).first()
        att = Attendance.query.filter_by(member_id=m.id, pujan_year=year).first()
        sks = Sankalp.query.filter_by(member_id=m.id, pujan_year=year).all()
        amount = sum(float(x.amount) for x in sks)
        rows.append({
            'id': m.id, 'member_code': m.member_code or '', 'name': m.display_name,
            'mobile': m.mobile or '', 'email': m.email or '',
            'center_id': m.center_id,
            'center_name': m.center.label if m.center else '',
            'pass_number': p.pass_number if p else '',
            'checked_in': bool(att),
            'sankalp_count': len(sks), 'sankalp_amount': amount,
            'marked_by': m.ho_marker.full_name if m.ho_marker else '',
            'marked_at': to_ist(m.ho_marked_at),
        })
        c = by_center.setdefault(m.center_id, {
            'center_name': m.center.label if m.center else '', 'count': 0,
            'checked_in': 0, 'amount': 0.0})
        c['count'] += 1
        c['checked_in'] += 1 if att else 0
        c['amount'] += amount
    return jsonify({'year': year, 'rows': rows,
                    'by_center': sorted(by_center.values(), key=lambda x: x['center_name']),
                    'total': len(rows)})


@app.route('/api/members/<int:mid>/detail')
@login_required
def api_member_detail(mid):
    m = Member.query.get_or_404(mid)
    denied = guard_center(m, 'member')
    if denied:
        return denied
    return jsonify(strip_money(m.to_dict()))


# ======================== API: SANKALP ========================
def _resolve_target(member, stype, data):
    """Work out which firm/company and partner/director a pledge is for, and
    which centre collects it.

    Returns (entity, person, collecting_center_id), or (error_string, None, None).
    """
    if stype == 'individual':
        return None, None, member.center_id

    wants = 'firm' if stype in ('firm', 'firm_partner') else 'company'
    pool = [e for e in member.entities if e.entity_type == wants]
    if not pool:
        return f'This member has no {wants} on record', None, None

    eid = data.get('entity_id')
    entity = next((e for e in pool if e.id == eid), None) if eid else None
    if entity is None:
        if eid:
            return 'That firm/company does not belong to this member', None, None
        if len(pool) > 1:
            return f'Choose which {wants} this pledge is for', None, None
        entity = pool[0]

    if stype in ('firm', 'company'):
        # In the entity's own name - the member's centre collects.
        return entity, None, member.center_id

    if not entity.people:
        role = 'partner' if wants == 'firm' else 'director'
        return f'This {wants} has no {role} on record', None, None
    pid = data.get('person_id')
    person = next((p for p in entity.people if p.id == pid), None) if pid else None
    if person is None:
        if pid:
            return 'That person does not belong to this firm/company', None, None
        if len(entity.people) > 1:
            role = 'partner' if wants == 'firm' else 'director'
            return f'Choose which {role} this pledge is for', None, None
        person = entity.people[0]

    # The partner's own centre collects, which may not be the firm's centre.
    return entity, person, (person.effective_center_id or member.center_id)


@app.route('/api/sankalps', methods=['GET'])
@perm_required('page.sankalps', 'page.collections', 'page.passes')
def api_get_sankalps():
    cf = get_user_center_filter()
    year = request.args.get('year', get_current_year(), type=int)
    q = Sankalp.query.filter_by(pujan_year=year)
    if cf: q = q.filter_by(center_id=cf)
    cid = request.args.get('center_id', type=int)
    if cid: q = q.filter_by(center_id=cid)
    search = (request.args.get('search') or '').strip()
    if search:
        # Several terms at once: "001, 003, 115966" finds all three. Handy when
        # the seats you want are not next to each other in the list.
        terms = [t.strip() for t in re.split(r'[,;\n]+', search) if t.strip()]
        if len(terms) > 1:
            groups = []
            for term in terms:
                groups.append(_sankalp_search_clause(term, year))
            q = q.filter(db.or_(*groups))
            search = None
    if search:
        q = q.filter(_sankalp_search_clause(search, year))
    rows = q.order_by(Sankalp.created_at.desc()).all()
    # The seat number for the new column on the sankalp list.
    seats = {a.member_id: a for a in Attendance.query.filter_by(pujan_year=year).all()}
    out = []
    for sk in rows:
        d = sk.to_dict(with_collection=True)
        att = seats.get(sk.member_id)
        d['seat_no'] = att.seat_no if att else None
        d['seat_label'] = att.seat_label if att else ''
        # So the double tick shows beside the seat number here as well, the
        # moment darshan is saved on the desk screen.
        d['darshan'] = bool(att.darshan_at) if att else False
        d['darshan_time'] = (to_ist(att.darshan_at, '%d-%m-%Y %H:%M')
                             if att and att.darshan_at else '')
        d['darshan_done'] = bool(att.darshan_done_at) if att else False
        d['darshan_done_time'] = (to_ist(att.darshan_done_at, '%d-%m-%Y %H:%M')
                                  if att and att.darshan_done_at else '')
        d['verified_at'] = to_ist(sk.verified_at, '%d-%m-%Y %H:%M')
        d['dharmada_type'] = (sk.member.dharmada_type or '') if sk.member else ''
        d['dharmada_amount'] = float(sk.member.dharmada_amount or 0) if sk.member else 0.0
        out.append(d)
    return jsonify([strip_money(d) for d in out])

@app.route('/api/sankalps', methods=['POST'])
@perm_required('sankalp.create')
def api_create_sankalp():
    data = request.get_json(silent=True) or {}
    user = User.query.get(session['user_id'])
    member = Member.query.get(data.get('member_id'))
    if not member:
        return jsonify({'error': 'Member not found'}), 404
    year = data.get('pujan_year', get_current_year())
    stype = data.get('sankalp_type', 'individual')

    # Once a member is ticked for Head Office, HO owns their pujan for that
    # year, so the centre stops entering pledges against them.
    if member.attend_at_ho and is_center_scoped(user):
        return jsonify({'error': f'{member.name} is marked to attend at Head Office. '
                                 f'Head Office will record the sankalp. '
                                 f'Untick them if that is wrong.'}), 403

    entity, person, collect_cid = _resolve_target(member, stype, data)
    if isinstance(entity, str):                      # helper returned an error
        return jsonify({'error': entity}), 400

    # A centre user may only raise a pledge their own centre will collect.
    if is_center_scoped(user) and collect_cid != user.center_id:
        return jsonify({'error': 'That pledge would be collected by another center'}), 403

    existing = Sankalp.query.filter_by(member_id=member.id, pujan_year=year,
                                       sankalp_type=stype,
                                       entity_id=entity.id if entity else None,
                                       person_id=person.id if person else None).first()
    if existing:
        return jsonify({'error': f'Sankalp already exists for {year} for this selection'}), 400

    sk = Sankalp(member_id=member.id,
                 center_id=collect_cid,               # who collects
                 owner_center_id=member.center_id,    # the primary
                 entity_id=entity.id if entity else None,
                 person_id=person.id if person else None,
                 pujan_year=year, sankalp_type=stype,
                 sankalp_name=data['sankalp_name'],
                 amount=Decimal(str(data['amount'])), created_by=user.id)
    db.session.add(sk); db.session.commit()
    audit('create', 'sankalp', sk, label=sk.sankalp_name, year=sk.pujan_year)
    db.session.commit()
    msg = (f"Jay Swaminarayan! {sk.sankalp_name} - Chopda-Pujan {year} Sankalp "
           f"for Rs.{data['amount']} registered. - SMVS")
    sent_via = send_notification(member, msg)
    return jsonify({'success': True, 'sankalp': sk.to_dict(with_collection=True), 'sent_via': sent_via})


@app.route('/api/sankalps/recent')
@perm_required('sankalp.fast_entry', 'page.sankalps')
def api_sankalps_recent():
    """Pledges this user entered for the year, read back from the audit trail so
    the fast-entry list survives closing the dialog."""
    uid = session['user_id']
    year = request.args.get('year', type=int) or get_current_year()
    logs = (AuditLog.query
            .filter(AuditLog.user_id == uid, AuditLog.entity_type == 'sankalp',
                    AuditLog.action == 'create', AuditLog.pujan_year == year)
            .order_by(AuditLog.created_at.desc()).limit(500).all())
    rows, seen = [], set()
    for lg in logs:
        if lg.entity_id in seen:
            continue
        seen.add(lg.entity_id)
        sk = db.session.get(Sankalp, lg.entity_id) if lg.entity_id else None
        if not sk:
            continue
        rows.append({
            'id': sk.id, 'name': sk.sankalp_name,
            'member_code': (sk.member.member_code or '') if sk.member else '',
            'member_name': sk.member.name if sk.member else '',
            'center_name': sk.center.label if sk.center else '',
            'amount': float(sk.amount), 'existing': False,
            'when': to_ist(lg.created_at, '%d-%m-%Y %H:%M'),
        })
    rows.reverse()
    return jsonify({'rows': rows, 'total': len(rows),
                    'amount': sum(r['amount'] for r in rows)})


@app.route('/api/sankalps/unverify', methods=['POST'])
@perm_required('sankalp.bulk_edit')
def api_unverify_sankalps():
    """Clears the bulk-edit confirmation, so these rows read as unsaved again."""
    ids = (request.get_json(silent=True) or {}).get('ids') or []
    if not ids:
        return jsonify({'error': 'Nothing to clear'}), 400
    n = Sankalp.query.filter(Sankalp.id.in_(ids)).update(
        {'verified_at': None, 'verified_by': None}, synchronize_session=False)
    db.session.commit()
    return jsonify({'success': True, 'cleared': n})


@app.route('/api/members/<int:mid>/dharmada', methods=['PUT'])
@perm_required('member.edit', 'sankalp.bulk_edit')
def api_set_dharmada(mid):
    """Dharmada from the bulk-edit grid. It belongs to the member, so one edit
    covers every pledge of theirs."""
    d = request.get_json(silent=True) or {}
    m = Member.query.get_or_404(mid)
    cf = get_user_center_filter()
    if cf and m.center_id != cf:
        return jsonify({'error': f'{m.name} belongs to another center'}), 403
    before = {'dharmada_type': m.dharmada_type,
              'dharmada_amount': float(m.dharmada_amount or 0)}
    if 'dharmada_type' in d:
        kind = (d.get('dharmada_type') or '').strip()
        if kind and kind not in ('R', 'I/R', 'P', 'N'):
            return jsonify({'error': 'Dharmada type must be R, I/R, P or N'}), 400
        m.dharmada_type = kind
    if 'dharmada_amount' in d:
        m.dharmada_amount = _money(d.get('dharmada_amount'))
    db.session.commit()
    audit('update', 'member', m, old=before, label=m.name,
          new={'dharmada_type': m.dharmada_type,
               'dharmada_amount': float(m.dharmada_amount or 0)})
    db.session.commit()
    return jsonify({'success': True, 'dharmada_type': m.dharmada_type or '',
                    'dharmada_amount': float(m.dharmada_amount or 0)})


@app.route('/api/members/<int:mid>/add-firm', methods=['POST'])
@perm_required('sankalp.bulk_edit', 'sankalp.create')
def api_add_firm(mid):
    """A firm that turns up during bulk edit: add it to the member and pledge
    for it in one step, without leaving the row."""
    data = request.get_json(silent=True) or {}
    m = Member.query.get_or_404(mid)
    cf = get_user_center_filter()
    if cf and m.center_id != cf:
        return jsonify({'error': f'{m.name} belongs to another center'}), 403
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Give the firm or company a name'}), 400
    kind = (data.get('entity_type') or 'firm').strip().lower()
    if kind not in ('firm', 'company'):
        kind = 'firm'
    year = int(data.get('pujan_year') or get_current_year())
    amount = _money(data.get('amount'))
    if amount <= 0:
        return jsonify({'error': 'Enter the sankalp amount'}), 400

    ent = next((e for e in m.entities
                if e.entity_type == kind and e.name.strip().lower() == name.lower()), None)
    if not ent:
        ent = Entity(entity_type=kind, name=name)
        m.entities.append(ent)
        if m.member_type == 'individual':
            m.member_type = kind
        db.session.flush()

    if Sankalp.query.filter_by(member_id=m.id, pujan_year=year,
                               entity_id=ent.id, person_id=None).first():
        return jsonify({'error': f'{name} already has a {year} sankalp'}), 400

    sk = Sankalp(member_id=m.id, center_id=m.center_id, owner_center_id=m.center_id,
                 entity_id=ent.id, pujan_year=year, sankalp_type=kind,
                 sankalp_name=name, amount=amount, created_by=session['user_id'])
    db.session.add(sk)
    db.session.commit()
    audit('create', 'sankalp', sk, label=name, year=year)
    db.session.commit()
    return jsonify({'success': True, 'sankalp': sk.to_dict(with_collection=True),
                    'entity': {'id': ent.id, 'name': ent.name, 'entity_type': ent.entity_type}})


@app.route('/api/sankalps/bulk', methods=['POST'])
@perm_required('sankalp.fast_entry')
def api_sankalps_bulk():
    """Fast entry for pledges: member, what it is for, amount.

    `target` is 'individual' or 'firm:<id>' / 'company:<id>' - the one dropdown
    the grid shows. Partner and director pledges need the full form, because
    they also decide which centre collects.
    """
    data = request.get_json(silent=True) or {}
    rows = data.get('rows') or []
    year = int(data.get('pujan_year') or get_current_year())
    user = User.query.get(session['user_id'])
    cf = get_user_center_filter()
    saved, errors = [], []
    seen = set()

    for i, r in enumerate(rows):
        line = r.get('_row', i + 1)
        if not r.get('member_id') and not (r.get('amount') or '').__str__().strip():
            continue
        m = Member.query.get(r.get('member_id') or 0)
        if not m:
            errors.append({'row': line, 'message': 'Pick a member first'}); continue
        if cf and m.center_id != cf and not m.attend_at_ho:
            errors.append({'row': line, 'message': f'{m.name} belongs to another center'}); continue
        if m.attend_at_ho and is_center_scoped(user):
            errors.append({'row': line,
                           'message': f'{m.name} is attending at Head Office'}); continue
        try:
            amount = Decimal(str(r.get('amount')).replace(',', '').strip())
        except (InvalidOperation, AttributeError, ValueError):
            errors.append({'row': line, 'message': f'Amount for {m.name} is not a number'}); continue
        if amount <= 0:
            errors.append({'row': line, 'message': f'Amount for {m.name} must be above zero'}); continue

        target = (r.get('target') or 'individual').strip()
        if target == 'individual':
            stype, entity = 'individual', None
        else:
            kind, _, eid = target.partition(':')
            entity = next((e for e in m.entities if str(e.id) == eid), None)
            if not entity:
                errors.append({'row': line, 'message': f'That firm/company is not on {m.name}'}); continue
            stype = entity.entity_type

        key = (m.id, year, stype, entity.id if entity else None)
        if key in seen:
            errors.append({'row': line, 'message': f'{m.name} is repeated in this batch'}); continue
        seen.add(key)
        if Sankalp.query.filter_by(member_id=m.id, pujan_year=year, sankalp_type=stype,
                                   entity_id=entity.id if entity else None,
                                   person_id=None).first():
            errors.append({'row': line,
                           'message': f'{m.name} already has this {year} sankalp'}); continue

        sk = Sankalp(member_id=m.id, center_id=m.center_id, owner_center_id=m.center_id,
                     entity_id=entity.id if entity else None, pujan_year=year,
                     sankalp_type=stype,
                     sankalp_name=(r.get('sankalp_name') or '').strip()
                                  or (entity.name if entity else m.name),
                     amount=amount, created_by=user.id)
        db.session.add(sk)
        db.session.flush()
        audit('create', 'sankalp', sk, label=sk.sankalp_name, year=year)
        saved.append({'row': line, 'id': sk.id, 'name': sk.sankalp_name,
                      'amount': float(amount)})

    if errors:
        db.session.rollback()
        return jsonify({'ok': False, 'saved': 0, 'errors': errors,
                        'would_save': len(saved)}), 400
    db.session.commit()
    return jsonify({'ok': True, 'saved': len(saved), 'rows': saved,
                    'total': sum(x['amount'] for x in saved), 'errors': []})


@app.route('/api/sankalps/<int:sid>', methods=['GET'])
@perm_required('page.sankalps', 'page.collections')
def api_get_sankalp(sid):
    sk = Sankalp.query.get_or_404(sid)
    cf = get_user_center_filter()
    if cf and sk.center_id != cf:
        return jsonify({'error': 'Permission denied'}), 403
    return jsonify(sk.to_dict(with_collection=True))


@app.route('/api/sankalps/<int:sid>', methods=['PUT'])
@perm_required('sankalp.edit')
def api_update_sankalp(sid):
    data = request.get_json(silent=True) or {}
    sk = Sankalp.query.get_or_404(sid)
    before = snapshot(sk, 'sankalp')
    user = User.query.get(session['user_id'])
    restricted = is_center_scoped(user)

    if restricted and sk.center_id != user.center_id:
        return jsonify({'error': 'Permission denied'}), 403
    if restricted and sk.member and sk.member.attend_at_ho:
        return jsonify({'error': f'{sk.member.name} is attending at Head Office. '
                                 f'Head Office handles this sankalp.'}), 403

    new_amount = Decimal(str(data.get('amount', sk.amount)))
    amount_changed = new_amount != sk.amount
    remark = (data.get('remark') or '').strip()

    if restricted:
        # Centre staff may revise the amount and nothing else. The member, the
        # firm, the pledge type, the year and the name are all fixed once the
        # pledge exists, so the record cannot be quietly repointed.
        blocked = [f for f in ('member_id', 'sankalp_type', 'entity_id', 'person_id',
                               'pujan_year', 'sankalp_name')
                   if f in data and str(data[f] or '') not in ('', str(getattr(sk, f, '')))]
        if blocked:
            return jsonify({'error': 'You can only change the amount. '
                                     'Ask an admin to change anything else.'}), 403
        if not amount_changed:
            return jsonify({'error': 'Change the amount, or press Cancel'}), 400

    if amount_changed:
        if new_amount <= 0:
            return jsonify({'error': 'Amount must be greater than zero'}), 400
        collected = sum(float(c.amount) for c in sk.collections)
        if float(new_amount) < collected:
            return jsonify({'error': f'Rs.{collected:,.0f} has already been collected. '
                                     f'The pledge cannot be set below that.'}), 400
        # A remark is the record of why a pledge was revised. Filling in an
        # amount for the first time - a row created at zero when someone was
        # registered at the door - is not a revision, so it is not demanded.
        if not remark and float(sk.amount or 0) > 0:
            return jsonify({'error': 'A remark is required when the amount changes'}), 400
        db.session.add(SankalpChange(sankalp_id=sk.id, old_amount=sk.amount,
                                     new_amount=new_amount, remark=remark,
                                     changed_by=user.id))
        sk.amount = new_amount

    if not restricted:
        stype = data.get('sankalp_type', sk.sankalp_type)
        if 'sankalp_type' in data or 'entity_id' in data or 'person_id' in data:
            entity, person, collect_cid = _resolve_target(sk.member, stype, data)
            if isinstance(entity, str):
                return jsonify({'error': entity}), 400
            sk.entity_id = entity.id if entity else None
            sk.person_id = person.id if person else None
            sk.center_id = collect_cid
            sk.owner_center_id = sk.member.center_id
        sk.sankalp_type = stype
        sk.sankalp_name = data.get('sankalp_name', sk.sankalp_name)

    # A bulk-edit confirmation is stamped, so the grid can show the row as
    # already done when it is reopened.
    if (data.get('remark') or '').strip().lower().startswith('bulk'):
        sk.verified_at = utcnow()
        sk.verified_by = session['user_id']
    db.session.commit()
    audit('update', 'sankalp', sk, old=before, new=snapshot(sk, 'sankalp'),
          label=sk.sankalp_name, year=sk.pujan_year)
    db.session.commit()
    return jsonify({'success': True, 'sankalp': sk.to_dict(with_collection=True)})


@app.route('/api/sankalps/<int:sid>/carry-forward', methods=['GET'])
@login_required
def api_carry_preview(sid):
    """What next year's pledge would look like, plus the full history.

    Collections are counted across EVERY centre that collected against this
    pledge line, so a partner in another centre is included.
    """
    sk = Sankalp.query.get_or_404(sid)
    target = request.args.get('to_year', type=int) or (sk.pujan_year + 1)
    history, lifetime = [], 0.0
    cur = sk
    seen = set()
    while cur and cur.id not in seen:
        seen.add(cur.id)
        got = sum(float(c.amount) for c in cur.collections)
        lifetime += got
        history.append({
            'id': cur.id, 'year': cur.pujan_year, 'amount': float(cur.amount),
            'collected': got, 'pending': float(cur.amount) - got,
            'sankalp_name': cur.sankalp_name,
            'collected_by': cur.center.label if cur.center else '',
        })
        cur = cur.carried_from
    existing = Sankalp.query.filter_by(
        member_id=sk.member_id, pujan_year=target, sankalp_type=sk.sankalp_type,
        entity_id=sk.entity_id, person_id=sk.person_id).first()
    return jsonify({
        'source': sk.to_dict(with_collection=True),
        'to_year': target,
        'history': list(reversed(history)),
        'lifetime_collected': lifetime,
        'already_exists': bool(existing),
        'suggested_amount': float(sk.amount),
    })


@app.route('/api/sankalps/<int:sid>/carry-forward', methods=['POST'])
@perm_required('sankalp.carry_forward')
def api_carry_forward(sid):
    """Roll a pledge into another year, keeping member, firm, partner, type,
    name and the collecting centre. Only the amount may differ."""
    data = request.get_json(silent=True) or {}
    sk = Sankalp.query.get_or_404(sid)
    user = User.query.get(session['user_id'])
    target = data.get('to_year') or (sk.pujan_year + 1)
    try:
        target = int(target)
    except (TypeError, ValueError):
        return jsonify({'error': 'Pick a valid year'}), 400
    if target == sk.pujan_year:
        return jsonify({'error': 'Pick a different year'}), 400
    if Sankalp.query.filter_by(member_id=sk.member_id, pujan_year=target,
                               sankalp_type=sk.sankalp_type, entity_id=sk.entity_id,
                               person_id=sk.person_id).first():
        return jsonify({'error': f'A {target} sankalp already exists for this selection'}), 400
    try:
        amount = Decimal(str(data.get('amount', sk.amount)))
    except Exception:
        return jsonify({'error': 'Amount is not a number'}), 400
    if amount <= 0:
        return jsonify({'error': 'Amount must be greater than zero'}), 400
    new = Sankalp(member_id=sk.member_id, center_id=sk.center_id,
                  owner_center_id=sk.owner_center_id, entity_id=sk.entity_id,
                  person_id=sk.person_id, pujan_year=target,
                  sankalp_type=sk.sankalp_type,
                  sankalp_name=data.get('sankalp_name') or sk.sankalp_name,
                  amount=amount, carried_from_id=sk.id, created_by=user.id)
    db.session.add(new); db.session.commit()
    return jsonify({'success': True, 'sankalp': new.to_dict(with_collection=True)})


def _carry_one(sk, target, user, amount=None, name=None):
    """Shared by the single and bulk paths. Returns (new_sankalp, reason)."""
    if target == sk.pujan_year:
        return None, 'same year'
    if Sankalp.query.filter_by(member_id=sk.member_id, pujan_year=target,
                               sankalp_type=sk.sankalp_type, entity_id=sk.entity_id,
                               person_id=sk.person_id).first():
        return None, f'already has a {target} sankalp'
    new = Sankalp(member_id=sk.member_id, center_id=sk.center_id,
                  owner_center_id=sk.owner_center_id, entity_id=sk.entity_id,
                  person_id=sk.person_id, pujan_year=target,
                  sankalp_type=sk.sankalp_type,
                  sankalp_name=name or sk.sankalp_name,
                  amount=Decimal(str(amount if amount is not None else sk.amount)),
                  carried_from_id=sk.id, created_by=user.id)
    db.session.add(new)
    return new, None


@app.route('/api/sankalps/bulk-carry-forward', methods=['POST'])
@perm_required('sankalp.carry_forward')
def api_bulk_carry_forward():
    """Roll a whole year forward in one pass instead of one row at a time.

    `mode='preview'` reports exactly what would happen and writes nothing.
    Amounts can be kept, scaled by a percentage, or set to what was actually
    collected last year.
    """
    data = request.get_json(silent=True) or {}
    user = User.query.get(session['user_id'])
    from_year = data.get('from_year') or get_current_year()
    to_year = data.get('to_year') or (int(from_year) + 1)
    try:
        from_year, to_year = int(from_year), int(to_year)
    except (TypeError, ValueError):
        return jsonify({'error': 'Pick valid years'}), 400
    if from_year == to_year:
        return jsonify({'error': 'Pick two different years'}), 400

    q = Sankalp.query.filter_by(pujan_year=from_year)
    if data.get('center_id'):
        q = q.filter_by(center_id=int(data['center_id']))
    ids = data.get('sankalp_ids')
    if ids:
        q = q.filter(Sankalp.id.in_([int(i) for i in ids]))
    sankalps = q.order_by(Sankalp.id).all()

    basis = data.get('amount_basis', 'same')       # same | collected | percent
    try:
        pct = float(data.get('percent', 100))
    except (TypeError, ValueError):
        pct = 100.0

    will, skip = [], []
    for sk in sankalps:
        collected = sum(float(c.amount) for c in sk.collections)
        if basis == 'collected':
            amount = collected
        elif basis == 'percent':
            amount = round(float(sk.amount) * pct / 100.0, 2)
        else:
            amount = float(sk.amount)
        row = {'id': sk.id, 'member_code': (sk.member.member_code or '') if sk.member else '',
               'member_name': sk.member.display_name if sk.member else '',
               'sankalp_name': sk.sankalp_name,
               'old_amount': float(sk.amount), 'collected': collected,
               'new_amount': amount,
               'center_name': sk.center.label if sk.center else ''}
        if amount <= 0:
            skip.append({**row, 'reason': 'amount would be zero'}); continue
        exists = Sankalp.query.filter_by(
            member_id=sk.member_id, pujan_year=to_year, sankalp_type=sk.sankalp_type,
            entity_id=sk.entity_id, person_id=sk.person_id).first()
        if exists:
            skip.append({**row, 'reason': f'already has a {to_year} sankalp'}); continue
        will.append(row)

    if data.get('mode') != 'commit':
        return jsonify({'dry_run': True, 'from_year': from_year, 'to_year': to_year,
                        'will_carry': will, 'skipped': skip,
                        'total_amount': sum(r['new_amount'] for r in will)})

    created = 0
    by_id = {sk.id: sk for sk in sankalps}
    for row in will:
        new, reason = _carry_one(by_id[row['id']], to_year, user, amount=row['new_amount'])
        if new:
            created += 1
    db.session.commit()
    return jsonify({'ok': True, 'dry_run': False, 'from_year': from_year,
                    'to_year': to_year, 'created': created, 'skipped': len(skip)})


@app.route('/api/sankalps/<int:sid>', methods=['DELETE'])
@perm_required('sankalp.delete')
def api_delete_sankalp(sid):
    sk = Sankalp.query.get_or_404(sid)
    cf = get_user_center_filter()
    if cf and sk.center_id != cf:
        return jsonify({'error': 'That sankalp belongs to another center'}), 403
    if cf and sk.collections.count():
        return jsonify({'error': 'Money has already been collected against this '
                                 'sankalp. Ask an admin to remove it.'}), 403
    audit('delete', 'sankalp', sk, old=snapshot(sk, 'sankalp'),
          label=sk.sankalp_name, year=sk.pujan_year)
    db.session.delete(sk); db.session.commit()
    return jsonify({'success': True})


# ======================== API: COLLECTIONS ========================
@app.route('/api/collections', methods=['GET'])
@perm_required('page.collections', 'page.sankalps')
def api_get_collections():
    cf = get_user_center_filter()
    year = request.args.get('year', get_current_year(), type=int)
    q = Collection.query.join(Sankalp).filter(Sankalp.pujan_year == year)
    if cf: q = q.filter(Collection.center_id == cf)
    cid = request.args.get('center_id', type=int)
    if cid: q = q.filter(Collection.center_id == cid)
    search = (request.args.get('search') or '').strip()
    if search:
        like = f'%{search}%'
        mem_sub = db.session.query(Member.id).filter(db.or_(
            Member.name.ilike(like), Member.member_code.ilike(like),
            Member.mobile.ilike(like)))
        sk_sub = db.session.query(Sankalp.id).filter(db.or_(
            Sankalp.sankalp_name.ilike(like), Sankalp.member_id.in_(mem_sub)))
        ctr_sub = db.session.query(Center.id).filter(db.or_(
            Center.name.ilike(like), Center.name_en.ilike(like), Center.city.ilike(like)))
        conds = [Collection.remarks.ilike(like), Collection.sankalp_id.in_(sk_sub),
                 Collection.center_id.in_(ctr_sub)]
        try:
            conds.append(Collection.amount == Decimal(search.replace(',', '').strip()))
        except (InvalidOperation, ValueError):
            pass
        q = q.filter(db.or_(*conds))
    return jsonify([c.to_dict() for c in q.order_by(Collection.collection_date.desc()).all()])

@app.route('/api/collections', methods=['POST'])
@perm_required('collection.create')
def api_create_collection():
    """Two ways of taking money.

    Firm wise  - against one pledge, as before.
    Individual - against a member as a whole: their firms are clubbed and the
                 amount is spread across their outstanding pledges, oldest
                 first, so the per-firm figures stay correct underneath.
    """
    data = request.get_json() or {}
    user = User.query.get(session['user_id'])
    mode = (data.get('mode') or 'firm').strip().lower()
    # The form no longer asks for a date; today is what is meant.
    raw_when = (data.get('collection_date') or '').strip()
    when = _parse_date(raw_when) if raw_when else date.today()
    remarks = data.get('remarks', '')
    try:
        amount = Decimal(str(data.get('amount')).replace(',', '').strip())
    except (InvalidOperation, ValueError, AttributeError):
        return jsonify({'error': 'Enter a valid amount'}), 400
    if amount <= 0:
        return jsonify({'error': 'Enter an amount above zero'}), 400

    if mode == 'individual':
        member = Member.query.get(data.get('member_id') or 0)
        if not member:
            return jsonify({'error': 'Pick a member'}), 404
        if is_center_scoped(user) and member.center_id != user.center_id:
            return jsonify({'error': 'That member belongs to another center'}), 403
        year = int(data.get('pujan_year') or get_current_year())
        pledges = Sankalp.query.filter_by(member_id=member.id, pujan_year=year)\
            .order_by(Sankalp.created_at).all()
        if not pledges:
            return jsonify({'error': f'{member.name} has no {year} sankalp'}), 400
        outstanding = sum(Decimal(str(p.amount)) - sum(Decimal(str(c.amount))
                          for c in p.collections) for p in pledges)
        if amount > outstanding:
            return jsonify({'error': f'Exceeds what is pending. '
                                     f'Max more: Rs.{float(outstanding):,.0f}'}), 400
        left, made = amount, []
        for p in pledges:
            if left <= 0:
                break
            pending = Decimal(str(p.amount)) - sum(Decimal(str(c.amount)) for c in p.collections)
            if pending <= 0:
                continue
            take = pending if pending < left else left
            col = Collection(sankalp_id=p.id, center_id=p.center_id, amount=take,
                             collection_date=when, remarks=remarks, created_by=user.id)
            db.session.add(col)
            made.append(col)
            left -= take
        db.session.commit()
        for col in made:
            audit('create', 'collection', col, label=f'{float(col.amount):,.0f}', year=year)
        db.session.commit()
        return jsonify({'success': True, 'split_across': len(made),
                        'collections': [c.to_dict() for c in made]})

    sk = Sankalp.query.get(data.get('sankalp_id') or 0)
    if not sk:
        return jsonify({'error': 'Sankalp not found'}), 404
    if is_center_scoped(user) and sk.center_id != user.center_id:
        return jsonify({'error': 'Permission denied'}), 403
    collected = sum(Decimal(str(c.amount)) for c in sk.collections)
    if collected + amount > Decimal(str(sk.amount)):
        return jsonify({'error': f'Exceeds sankalp. Max more: '
                                 f'Rs.{float(Decimal(str(sk.amount)) - collected):,.0f}'}), 400
    col = Collection(sankalp_id=sk.id, center_id=sk.center_id, amount=amount,
                     collection_date=when, remarks=remarks, created_by=user.id)
    db.session.add(col)
    db.session.commit()
    audit('create', 'collection', col, label=f'{float(col.amount):,.0f}', year=sk.pujan_year)
    db.session.commit()
    return jsonify({'success': True, 'collection': col.to_dict()})


@app.route('/api/collections/targets')
@perm_required('collection.create', 'page.collections')
def api_collection_targets():
    """What the Select Sankalp list offers, for either mode.

    Individual clubs a member's firms into one line; firm wise lists each
    pledge separately. Both carry the amount, what has come in and what is
    still outstanding, so the form can show it the moment one is picked.
    """
    year = request.args.get('year', get_current_year(), type=int)
    mode = (request.args.get('mode') or 'firm').strip().lower()
    cf = get_user_center_filter()
    cid = cf or request.args.get('center_id', type=int)
    q = Sankalp.query.filter_by(pujan_year=year)
    if cid:
        q = q.filter_by(center_id=cid)
    rows = q.all()

    if mode == 'individual':
        grouped = {}
        for sk in rows:
            g = grouped.setdefault(sk.member_id, {
                'id': sk.member_id,
                'label': sk.member.name if sk.member else '',
                'member_code': (sk.member.member_code or '') if sk.member else '',
                'amount': Decimal('0'), 'collected': Decimal('0'), 'parts': 0})
            g['amount'] += Decimal(str(sk.amount))
            g['collected'] += sum(Decimal(str(c.amount)) for c in sk.collections)
            g['parts'] += 1
        out = []
        for g in grouped.values():
            out.append({'id': g['id'], 'label': g['label'], 'member_code': g['member_code'],
                        'amount': float(g['amount']), 'collected': float(g['collected']),
                        'pending': float(g['amount'] - g['collected']), 'parts': g['parts']})
        out.sort(key=lambda x: x['label'])
        return jsonify({'mode': 'individual', 'year': year, 'rows': out})

    out = []
    for sk in rows:
        got = sum(Decimal(str(c.amount)) for c in sk.collections)
        out.append({'id': sk.id, 'label': sk.sankalp_name,
                    'member_code': (sk.member.member_code or '') if sk.member else '',
                    'member_name': sk.member.name if sk.member else '',
                    'amount': float(sk.amount), 'collected': float(got),
                    'pending': float(Decimal(str(sk.amount)) - got), 'parts': 1})
    out.sort(key=lambda x: (x['member_name'], x['label']))
    return jsonify({'mode': 'firm', 'year': year, 'rows': out})

@app.route('/api/collections/<int:colid>', methods=['DELETE'])
@perm_required('collection.delete')
def api_delete_collection(colid):
    col = Collection.query.get_or_404(colid)
    cf = get_user_center_filter()
    if cf and col.center_id != cf:
        return jsonify({'error': 'That collection belongs to another center'}), 403
    audit('delete', 'collection', col, old=snapshot(col, 'collection'),
          label=f'{float(col.amount):,.0f}')
    db.session.delete(col); db.session.commit()
    return jsonify({'success': True})


# ======================== API: PASSES ========================
@app.route('/api/pass-config', methods=['GET'])
@login_required
def api_get_pass_config():
    year = request.args.get('year', get_current_year(), type=int)
    config = PassConfig.query.filter_by(pujan_year=year).first()
    if not config:
        config = PassConfig(pujan_year=year); db.session.add(config); db.session.commit()
    return jsonify(config.to_dict())

@app.route('/api/pass-config', methods=['POST'])
@perm_required('pass.config')
def api_set_pass_config():
    data = request.get_json()
    user = User.query.get(session['user_id'])
    year = data.get('pujan_year', get_current_year())
    config = PassConfig.query.filter_by(pujan_year=year).first()
    if not config:
        config = PassConfig(pujan_year=year)
        db.session.add(config)
        # Flush so the column defaults are applied. Without this a brand-new
        # config has None prefixes and the "must be different" check fires on
        # a perfectly valid first save.
        db.session.flush()
    for f in ('yajman_start', 'guruji_start', 'general_start'):
        if data.get(f) is not None:
            try:
                setattr(config, f, int(data[f]))
            except (TypeError, ValueError):
                return jsonify({'error': f'{f} must be a number'}), 400
    for f in ('yajman_prefix', 'guruji_prefix', 'general_prefix'):
        if data.get(f):
            setattr(config, f, str(data[f]).strip().upper()[:10])
    prefixes = [(config.yajman_prefix or 'YJ'), (config.guruji_prefix or 'GU'),
                (config.general_prefix or 'GN')]
    if len(set(prefixes)) != 3:
        return jsonify({'error': 'The three prefixes must be different: '
                                 + ', '.join(prefixes)}), 400
    # Overlapping ranges would make a bare number ambiguous at the desk.
    starts = {'Yajman': config.yajman_start or 501,
              'Guruji': config.guruji_start or 1001,
              'General': config.general_start or 1501}
    if len(set(starts.values())) != 3:
        return jsonify({'error': 'The three starting numbers must be different: '
                                 + ', '.join(f'{k} {v}' for k, v in starts.items())}), 400
    config.updated_by = user.id
    db.session.commit()
    return jsonify({'success': True, 'config': config.to_dict()})

@app.route('/api/passes', methods=['GET'])
@perm_required('page.passes', 'page.attendance')
def api_get_passes():
    """Passes for the year, each with the seat number if its holder has already
    been checked in - the list shows both side by side."""
    cf = get_user_center_filter()
    year = request.args.get('year', get_current_year(), type=int)
    q = Pass.query.filter_by(pujan_year=year)
    if cf: q = q.filter_by(center_id=cf)
    cid = request.args.get('center_id', type=int)
    if cid: q = q.filter_by(center_id=cid)
    ptype = request.args.get('pass_type')
    if ptype: q = q.filter_by(pass_type=ptype)
    search = (request.args.get('search') or '').strip()
    if search:
        like = f'%{search}%'
        mem_sub = db.session.query(Member.id).filter(db.or_(
            Member.name.ilike(like), Member.member_code.ilike(like),
            Member.mobile.ilike(like)))
        q = q.filter(db.or_(Pass.pass_number.ilike(like), Pass.member_id.in_(mem_sub)))
    rows = q.order_by(Pass.pass_number).all()
    # Seat numbers, so the list can show the pass and the seat side by side.
    seats = {a.member_id: a for a in Attendance.query.filter_by(pujan_year=year).all()}
    out = []
    for p in rows:
        d = p.to_dict()
        att = seats.get(p.member_id)
        d['seat_no'] = att.seat_no if att else None
        d['seat_label'] = att.seat_label if att else ''
        d['printed'] = (p.print_count or 0) > 0
        d['print_count'] = p.print_count or 0
        d['last_printed'] = to_ist(p.last_printed_at, '%d-%m-%Y %H:%M')
        out.append(d)
    return jsonify([strip_money(d) for d in out])

@app.route('/api/integrity')
@perm_required('page.audit', 'page.users')
def api_integrity():
    """Looks for duplicate seats, duplicate pass numbers and people counted
    twice, and says whether the unique indexes are actually in place.

    The constraints prevent all of this going forward. Data entered before they
    existed is the reason this check is here - and the reason the boot-time
    version below reports rather than silently failing to create an index.
    """
    year = request.args.get('year', get_current_year(), type=int)
    problems = []

    dup_seats = (db.session.query(Attendance.seat_no, db.func.count(Attendance.id))
                 .filter(Attendance.pujan_year == year, Attendance.seat_no.isnot(None))
                 .group_by(Attendance.seat_no).having(db.func.count(Attendance.id) > 1).all())
    for seat, n in dup_seats:
        who = [a.member.name if a.member else '?' for a in
               Attendance.query.filter_by(pujan_year=year, seat_no=seat).all()]
        problems.append({'kind': 'duplicate seat', 'value': f'{seat:03d}',
                         'count': n, 'who': who})

    dup_pass = (db.session.query(Pass.pass_number, db.func.count(Pass.id))
                .filter(Pass.pujan_year == year)
                .group_by(Pass.pass_number).having(db.func.count(Pass.id) > 1).all())
    for num, n in dup_pass:
        who = [p.member.name if p.member else '?' for p in
               Pass.query.filter_by(pujan_year=year, pass_number=num).all()]
        problems.append({'kind': 'duplicate pass number', 'value': num,
                         'count': n, 'who': who})

    dup_att = (db.session.query(Attendance.member_id, db.func.count(Attendance.id))
               .filter(Attendance.pujan_year == year)
               .group_by(Attendance.member_id).having(db.func.count(Attendance.id) > 1).all())
    for mid, n in dup_att:
        m = db.session.get(Member, mid)
        problems.append({'kind': 'checked in more than once',
                         'value': m.name if m else str(mid), 'count': n, 'who': []})

    no_seat = Attendance.query.filter(Attendance.pujan_year == year,
                                      Attendance.seat_no.is_(None)).count()
    if no_seat:
        problems.append({'kind': 'check-in with no seat', 'value': '',
                         'count': no_seat, 'who': []})

    return jsonify({'year': year, 'ok': not problems, 'problems': problems,
                    'indexes': unique_indexes_present(),
                    'checked': {'seats': Attendance.query.filter_by(pujan_year=year).count(),
                                'passes': Pass.query.filter_by(pujan_year=year).count()}})


def unique_indexes_present():
    """Whether the constraints that prevent duplicates are really on the table.

    Worth asking rather than assuming: `CREATE UNIQUE INDEX IF NOT EXISTS` fails
    when the table already holds duplicates, and the boot code swallows that so
    an upgrade does not crash. This is how you find out it was swallowed.
    """
    out = {}
    try:
        insp = db.inspect(db.engine)
        for table, wanted in (('attendance', ('uq_attendance_seat_year',
                                              'uq_attendance_member_year')),
                              ('passes', ('uq_pass_year_number',))):
            names = set()
            for ix in insp.get_indexes(table):
                if ix.get('unique'):
                    names.add(ix.get('name'))
            for c in insp.get_unique_constraints(table):
                names.add(c.get('name'))
            for w in wanted:
                out[w] = w in names
    except Exception as e:
        out['error'] = f'{e.__class__.__name__}: {e}'
    return out


@app.route('/api/passes/print-check')
@login_required
def api_pass_print_check():
    """Which of these passes have been printed already, and when - so a second
    print asks first instead of quietly making a duplicate."""
    ids = [int(x) for x in (request.args.get('ids') or '').split(',') if x.strip().isdigit()]
    year = request.args.get('year', get_current_year(), type=int)
    q = Pass.query.filter_by(pujan_year=year)
    cf = get_user_center_filter()
    if cf:
        q = q.filter_by(center_id=cf)
    if ids:
        q = q.filter(Pass.id.in_(ids))
    seats = {a.member_id: a for a in Attendance.query.filter_by(pujan_year=year).all()}
    done = []
    for p in q.all():
        if (p.print_count or 0) > 0:
            att = seats.get(p.member_id)
            done.append({'id': p.id, 'pass_number': p.pass_number,
                         'seat_label': att.seat_label if att else '',
                         'member_name': p.member.name if p.member else '',
                         'count': p.print_count,
                         'when': to_ist(p.last_printed_at, '%d-%m-%Y %H:%M')})
    return jsonify({'printed': done, 'count': len(done)})


@app.route('/api/passes/candidates')
@perm_required('pass.generate')
def api_pass_candidates():
    """Who could get a pass, with their pledge total and whether one already
    exists for this year and type.

    center_id is optional - leaving it out means every centre, which is what
    the desk usually wants when generating by amount band rather than by area.
    """
    year = request.args.get('year', get_current_year(), type=int)
    ptype = request.args.get('pass_type', 'yajman')
    cf = get_user_center_filter()
    q = Member.query.filter_by(is_active=True)
    if cf:
        q = q.filter_by(center_id=cf)
    cid = request.args.get('center_id', type=int)
    if cid:
        q = q.filter_by(center_id=cid)

    def _dec(name):
        raw = (request.args.get(name) or '').replace(',', '').strip()
        if not raw:
            return None
        try:
            return Decimal(raw)
        except (InvalidOperation, ValueError):
            return None

    lo, hi = _dec('min_amount'), _dec('max_amount')
    only_without = request.args.get('only_without') == '1'

    rows = []
    for m in q.order_by(Member.name).all():
        sks = Sankalp.query.filter_by(member_id=m.id, pujan_year=year).all()
        total = sum(Decimal(str(s_.amount)) for s_ in sks) if sks else Decimal('0')
        if lo is not None and total < lo:
            continue
        if hi is not None and total > hi:
            continue
        existing = Pass.query.filter_by(member_id=m.id, pujan_year=year,
                                        pass_type=ptype).first()
        any_pass = Pass.query.filter_by(member_id=m.id, pujan_year=year).first()
        if only_without and existing:
            continue
        rows.append({
            'id': m.id, 'member_code': m.member_code or '',
            'name': m.display_name, 'mobile': m.mobile or '',
            'center_id': m.center_id,
            'center_name': m.center.label if m.center else '',
            'sankalp_amount': float(total), 'sankalp_count': len(sks),
            'sankalp_id': sks[0].id if sks else None,
            'has_pass': bool(existing),
            'pass_number': existing.pass_number if existing else '',
            'other_pass': (any_pass.pass_number if (any_pass and not existing) else ''),
            'other_pass_type': (any_pass.pass_type if (any_pass and not existing) else ''),
            'attend_at_ho': bool(m.attend_at_ho),
        })
    return jsonify({'year': year, 'pass_type': ptype, 'rows': rows,
                    'total': len(rows),
                    'already': sum(1 for r in rows if r['has_pass'])})


@app.route('/api/passes/generate', methods=['POST'])
@perm_required('pass.generate')
def api_generate_passes():
    """Passes for many members. Retried on a numbering clash, because two
    people pressing Generate together would otherwise leave one with an
    error and no passes."""
    for attempt in range(4):
        try:
            return _generate_passes_once()
        except IntegrityError:
            db.session.rollback()
            if attempt == 3:
                return jsonify({'error': 'Passes were being generated at the '
                                         'same moment by someone else. '
                                         'Press Generate again.'}), 409


def _generate_passes_once():
    data = request.get_json()
    user = User.query.get(session['user_id'])
    year = data.get('pujan_year', get_current_year())
    pass_type = data['pass_type']
    member_ids = data['member_ids']
    sankalp_ids = data.get('sankalp_ids', [])
    if pass_type not in PASS_TYPES:
        return jsonify({'error': 'Unknown pass type'}), 400

    config = get_pass_config(year)
    prefix = config.prefix_for(pass_type)
    start = config.start_for(pass_type)

    # Highest number already issued this year for this type. Sorting on the
    # numeric suffix (not on id) keeps the sequence correct even if rows were
    # inserted out of order.
    issued = Pass.query.filter_by(pujan_year=year, pass_type=pass_type).all()
    next_num = start
    for p in issued:
        try:
            v = int(p.pass_number.replace(prefix, ''))
        except ValueError:
            continue
        if v >= next_num:
            next_num = v + 1

    created = []
    for i, mid in enumerate(member_ids):
        member = Member.query.get(mid)
        if not member:
            continue
        if is_center_scoped(user) and member.center_id != user.center_id:
            continue
        if Pass.query.filter_by(member_id=mid, pujan_year=year, pass_type=pass_type).first():
            continue
        # next_num advances ONLY when a pass is actually created, so skipped
        # members no longer punch holes in the sequence.
        pass_num = f"{prefix}{str(next_num).zfill(4)}"
        next_num += 1
        sk_id = sankalp_ids[i] if i < len(sankalp_ids) else None
        p = Pass(member_id=mid, sankalp_id=sk_id, center_id=member.center_id,
                 pujan_year=year, pass_number=pass_num, pass_type=pass_type, created_by=user.id)
        db.session.add(p); created.append(p)
    db.session.commit()
    return jsonify({'success': True, 'passes': [p.to_dict() for p in created], 'count': len(created)})

@app.route('/api/passes/<int:pid>', methods=['DELETE'])
@perm_required('pass.generate')
def api_delete_pass(pid):
    p = Pass.query.get_or_404(pid)
    denied = guard_center(p, 'pass')
    if denied:
        return denied
    # Remove the check-in too, otherwise the row is orphaned and the centre
    # summary counts attendance against a pass that no longer exists.
    Attendance.query.filter_by(pass_id=p.id).delete(synchronize_session=False)
    db.session.delete(p)
    db.session.commit()
    return jsonify({'success': True})


# ======================== API: ATTENDANCE ========================
def slip_data(att):
    """Everything one slip prints, gathered in one place so the two versions
    cannot drift apart."""
    m = att.member
    year = att.pujan_year
    sks = Sankalp.query.filter_by(member_id=m.id, pujan_year=year).all() if m else []
    total = sum(Decimal(str(x.amount)) for x in sks) if sks else Decimal('0')
    # The firm this pledge is for, if any, and its number for the year.
    entity = next((x.entity for x in sks if x.entity_id), None)
    lines = [{'name': (x.entity.name if x.entity else (m.name if m else '')),
              'type': x.sankalp_type,
              'firm_no': firm_number_for(x.entity_id, year) if x.entity_id else None,
              'amount': float(x.amount)} for x in sks]
    if not lines and m:
        # Registered at the door with no pledge yet - their firms still belong
        # on the slip so the desk can see who it is for.
        lines = [{'name': e.name, 'type': e.entity_type,
                  'firm_no': firm_number_for(e.id, year), 'amount': None}
                 for e in m.entities]
    cfg = get_event_config(year)
    return {
        'id': att.id,
        'seat_no': att.seat_no, 'seat_label': att.seat_label,
        'pass_number': att.pas.pass_number if att.pas else '',
        'pass_type': att.pas.pass_type if att.pas else '',
        'member_id': m.member_code if m else '',
        'member_name': m.name if m else '',
        'mobile': (m.mobile or '') if m else '',
        'center_name': att.center.label if att.center else '',
        'firm_name': entity.name if entity else '',
        'firm_no': firm_number_for(entity.id, year) if entity else None,
        'lines': lines,
        'amount': float(total),
        'sankalp_count': len(sks),
        'pujan_year': year,
        'check_in_time': to_ist(att.check_in_time, '%d-%m-%Y %H:%M'),
        'printed': bool(att.slip_printed_at),
        'event_date': cfg.event_date.strftime('%d-%m-%Y') if cfg and cfg.event_date else '',
        'venue': ((att.center.label if att.center else '') or
                  (cfg.center_venue or '') if cfg else ''),
    }


@app.route('/api/attendance/<int:aid>/slip')
@perm_required('slip.print', 'attendance.checkin')
def api_slip_data(aid):
    att = Attendance.query.get_or_404(aid)
    cf = get_user_center_filter()
    if cf and att.center_id != cf:
        return jsonify({'error': 'That check-in belongs to another center'}), 403
    data = slip_data(att)
    db.session.commit()                 # any firm number just created
    return jsonify(data)


@app.route('/api/attendance/slips')
@perm_required('slip.print')
def api_slips_queue():
    """Check-ins waiting to be printed, so one person can check people in and
    another can run the printer."""
    year = request.args.get('year', get_current_year(), type=int)
    q = Attendance.query.filter_by(pujan_year=year)
    cf = get_user_center_filter()
    if cf:
        q = q.filter_by(center_id=cf)
    if request.args.get('pending') == '1':
        q = q.filter(Attendance.slip_printed_at.is_(None))
    rows = q.order_by(Attendance.seat_no).all()
    out = [slip_data(a) for a in rows]
    db.session.commit()
    return jsonify({'year': year, 'rows': out, 'total': len(out),
                    'pending': sum(1 for r in out if not r['printed'])})


@app.route('/api/attendance/slips/printed', methods=['POST'])
@perm_required('slip.print')
def api_mark_slips_printed():
    ids = (request.get_json(silent=True) or {}).get('ids') or []
    if not ids:
        return jsonify({'error': 'Nothing to mark'}), 400
    n = Attendance.query.filter(Attendance.id.in_(ids)).update(
        {'slip_printed_at': utcnow(), 'slip_printed_by': session['user_id']},
        synchronize_session=False)
    db.session.commit()
    return jsonify({'success': True, 'marked': n})


@app.route('/slips/print')
@perm_required('slip.print')
def slips_print_page():
    """The printable sheet. Two A6 slips per check-in: one with the sankalp
    amount, one without."""
    year = request.args.get('year', get_current_year(), type=int)
    ids = [int(x) for x in (request.args.get('ids') or '').split(',') if x.strip().isdigit()]
    q = Attendance.query.filter_by(pujan_year=year)
    cf = get_user_center_filter()
    if cf:
        q = q.filter_by(center_id=cf)
    if ids:
        q = q.filter(Attendance.id.in_(ids))
    elif request.args.get('pending') == '1':
        q = q.filter(Attendance.slip_printed_at.is_(None))
    rows = [slip_data(a) for a in q.order_by(Attendance.seat_no).all()]
    db.session.commit()
    cfg = get_event_config(year)
    return render_template('slips_print.html', slips=rows, year=year,
                           event=cfg, lang=session.get('lang', 'en'),
                           auto_mark=request.args.get('mark') == '1')


@app.route('/api/attendance', methods=['GET'])
@perm_required('attendance.list')
def api_get_attendance():
    cf = get_user_center_filter()
    year = request.args.get('year', get_current_year(), type=int)
    q = Attendance.query.filter_by(pujan_year=year)
    if cf: q = q.filter_by(center_id=cf)
    cid = request.args.get('center_id', type=int)
    if cid: q = q.filter_by(center_id=cid)
    return jsonify([r.to_dict() for r in q.order_by(Attendance.check_in_time.desc()).all()])

def resolve_pass(raw, year):
    """Turn whatever the desk typed (or the scanner read) into one pass.

    Accepts, for a year whose ranges are YJ 501+, GU 1001+, GN 1501+:
        "YJ0501" / "yj 501" / "YJ-501"   -> exact, prefix given
        "501"                            -> the only pass numbered 501
        "1001"                           -> the Guruji pass
    The three ranges do not overlap, so a bare number is unambiguous. If it
    somehow is ambiguous the desk is told to add the prefix rather than being
    given the wrong person.

    Returns (pass, error_message).
    """
    txt = re.sub(r'[^A-Za-z0-9]', '', str(raw or '')).upper()
    if not txt:
        return None, 'Enter a pass number'

    # A QR scan gives the full code, which lands here as prefix + digits.
    m = re.match(r'^([A-Z]+)(\d+)$', txt)
    if m:
        prefix, digits = m.group(1), m.group(2)
        n = int(digits)
        candidates = Pass.query.filter_by(pujan_year=year).filter(
            Pass.pass_number.ilike(f'{prefix}%')).all()
        for p in candidates:
            tail = re.sub(r'^[A-Za-z]+', '', p.pass_number)
            if tail.isdigit() and int(tail) == n:
                return p, None
        # Exact string match as a fallback for hand-set numbers.
        exact = Pass.query.filter_by(pujan_year=year, pass_number=txt).first()
        if exact:
            return exact, None
        return None, f'Pass "{raw}" not found for {year}'

    if txt.isdigit():
        n = int(txt)
        hits = []
        for p in Pass.query.filter_by(pujan_year=year).all():
            tail = re.sub(r'^[A-Za-z]+', '', p.pass_number)
            if tail.isdigit() and int(tail) == n:
                hits.append(p)
        if len(hits) == 1:
            return hits[0], None
        if len(hits) > 1:
            return None, ('Number ' + txt + ' matches ' +
                          ', '.join(sorted(h.pass_number for h in hits)) +
                          '. Type the full number.')
        return None, f'No pass numbered {txt} in {year}'

    exact = Pass.query.filter_by(pujan_year=year, pass_number=txt).first()
    return (exact, None) if exact else (None, f'Pass "{raw}" not found for {year}')


@app.route('/api/passes/lookup')
@perm_required('attendance.checkin', 'page.passes')
def api_pass_lookup():
    """Used by the check-in box to show who a number belongs to as it is typed,
    before anything is committed."""
    year = request.args.get('year', get_current_year(), type=int)
    p, err = resolve_pass(request.args.get('q', ''), year)
    if err:
        return jsonify({'found': False, 'error': err})
    return jsonify({'found': True, 'pass': p.to_dict()})


def send_checkin_message(member, year):
    """A message the moment someone is checked in.

    Kept quiet on purpose: a gate that stops working because an SMS gateway is
    slow or down would be far worse than a missed message, so anything that
    goes wrong here is logged and swallowed.
    """
    if os.environ.get('CHECKIN_SMS', '1') != '1':
        return None
    tpl = (MessageTemplate.query
           .filter_by(purpose='checkin', is_active=True)
           .filter(db.or_(MessageTemplate.pujan_year == year,
                          MessageTemplate.pujan_year.is_(None)))
           .order_by(MessageTemplate.pujan_year.desc().nullslast()).first())
    if not tpl or not member or not (member.mobile or ''):
        return None
    channel = (tpl.channel if tpl.channel in ('sms', 'whatsapp') else 'sms')
    try:
        ctx = comm_context(member, year)
        body = notify.render(tpl.body, ctx)
        log = CommLog(member_id=member.id, comm_type=channel,
                      destination=member.mobile, subject='', message=body,
                      status='queued', pujan_year=year, template_id=tpl.id,
                      audience='ho' if member.attend_at_ho else 'center')
        db.session.add(log)
        items = [{'phone': member.mobile, 'message': body}]
        if channel == 'sms' and notify.sms_configured():
            res = notify.send_sms_via_provider(items)[0]
        elif channel == 'whatsapp' and notify.whatsapp_configured():
            res = notify.send_whatsapp_via_provider(items)[0]
        else:
            res = {'ok': False, 'error': f'No {channel} gateway configured'}
        log.status = 'sent' if res.get('ok') else 'failed'
        log.error = res.get('error')
        db.session.commit()
        return {'channel': channel, 'sent': bool(res.get('ok')),
                'error': res.get('error')}
    except Exception as e:
        db.session.rollback()
        app.logger.warning('Check-in message failed for %s: %s', member.id, e)
        return {'channel': channel, 'sent': False, 'error': str(e)[:120]}


@app.route('/api/checkin-template')
@perm_required('attendance.checkin', 'comm.templates_view')
def api_checkin_template():
    """Whether a check-in message is set up, for the screen to say so."""
    year = request.args.get('year', get_current_year(), type=int)
    tpl = (MessageTemplate.query
           .filter_by(purpose='checkin', is_active=True)
           .filter(db.or_(MessageTemplate.pujan_year == year,
                          MessageTemplate.pujan_year.is_(None))).first())
    ready = {'sms': notify.sms_configured(), 'whatsapp': notify.whatsapp_configured()}
    return jsonify({'template': tpl.to_dict() if tpl else None,
                    'enabled': os.environ.get('CHECKIN_SMS', '1') == '1',
                    'gateways': ready})


def resolve_darshan_target(raw, year):
    """The person behind whatever was scanned or typed, for Guruji Darshan.

    Accepts a member ID, a pass number or bare pass digits (what the scanner
    reads), or a seat number - the desk uses whichever is in front of it. The
    member ID is tried first because it is unique and typed deliberately,
    where a bare number could be read as either.

    Darshan is only ever recorded against somebody already checked in. The
    tick sits beside the seat number they were given on arrival, so a person
    with no seat is something to say out loud - never a reason to hand out a
    second seat, which is exactly what a re-scan used to do.

    Returns (member, attendance, error).
    """
    txt = (str(raw or '')).strip()
    if not txt:
        return None, None, 'Enter a member ID, pass number or seat number'

    code = re.sub(r'\s+', '', txt).upper()
    member = Member.query.filter(db.func.upper(Member.member_code) == code).first()
    if member is None:
        p, _ = resolve_pass(txt, year)
        if p:
            member = p.member
    if member is None and code.isdigit():
        seated = Attendance.query.filter_by(pujan_year=year,
                                            seat_no=int(code)).first()
        if seated:
            member = seated.member
    if member is None:
        return None, None, f'Nothing found for "{raw}" in {year}'

    att = Attendance.query.filter_by(member_id=member.id,
                                     pujan_year=year).first()
    if not att:
        return member, None, (f'{member.display_name} has not been checked in '
                              f'yet. Check them in first - darshan is marked '
                              f'against their seat number.')
    return member, att, None


def can_edit_dharmada(user):
    """The same pair of rights the dharmada route itself accepts."""
    return has_perm(user, 'member.edit') or has_perm(user, 'sankalp.bulk_edit')


def darshan_target_from_request(data, year):
    """Whoever the darshan desk means, by member id or by what was typed.

    The queue hands back a member id, which is exact - no need to put it back
    through the text resolver and hope the member code is unique. Anything
    typed or scanned still goes the long way round.
    """
    mid = data.get('member_id')
    if mid:
        member = Member.query.get(mid)
        if not member:
            return None, None, 'Member not found'
        att = Attendance.query.filter_by(member_id=member.id,
                                         pujan_year=year).first()
        if not att:
            return member, None, (f'{member.display_name} has not been checked '
                                  f'in yet. Check them in first - darshan is '
                                  f'marked against their seat number.')
        return member, att, None
    raw = (data.get('code') or data.get('pass_number') or '').strip()
    return resolve_darshan_target(raw, year)


def mark_darshan(att, user):
    """Stage one, at the attendance desk. Returns True the first time only.

    The seat number is untouched - that is the whole point of the second scan.
    """
    if att.darshan_at:
        return False
    att.darshan_at = utcnow()
    att.darshan_by = user.id
    db.session.commit()
    audit('update', 'attendance', att,
          label=att.member.display_name if att.member else '',
          new={'darshan': True, 'seat_no': att.seat_no})
    db.session.commit()
    return True


def mark_darshan_done(att, user):
    """Stage two, at Guruji's desk. Returns True the first time only.

    Somebody reached here without the attendance desk having scanned them a
    second time - looked up by hand, say - plainly did go to darshan, so stage
    one is stamped as well rather than leaving a row that is finished but never
    started.
    """
    if att.darshan_done_at:
        return False
    now = utcnow()
    if not att.darshan_at:
        att.darshan_at = now
        att.darshan_by = user.id
    att.darshan_done_at = now
    att.darshan_done_by = user.id
    db.session.commit()
    audit('update', 'attendance', att,
          label=att.member.display_name if att.member else '',
          new={'darshan_done': True, 'seat_no': att.seat_no})
    db.session.commit()
    return True


@app.route('/api/attendance/darshan', methods=['POST'])
@perm_required('attendance.darshan')
def api_mark_darshan():
    """Marks Guruji Darshan for someone already checked in.

    Deliberately not part of the check-in route: that one hands out a seat, and
    the whole point here is that a second scan must not. Scanning a person who
    has already been ticked is a confirmation rather than an error - at a busy
    darshan queue the same pass gets read twice all the time, and shouting at
    the volunteer for it would be wrong.
    """
    data = request.get_json(silent=True) or {}
    raw = (data.get('pass_number') or data.get('code') or '').strip()
    year = data.get('pujan_year', get_current_year())
    user = User.query.get(session['user_id'])
    member, att, err = resolve_darshan_target(raw, year)
    if err:
        return jsonify({'error': err}), 404 if att is None and member is None else 400
    cf = get_user_center_filter()
    if cf and att.center_id != cf:
        return jsonify({'error': 'That person belongs to another center'}), 403
    first = mark_darshan(att, user)
    return jsonify({'success': True, 'already': not first,
                    'member': member.display_name,
                    'member_code': member.member_code or '',
                    'seat_no': att.seat_no, 'seat_label': att.seat_label,
                    'attendance': att.to_dict()})


@app.route('/api/darshan/queue')
@perm_required('attendance.darshan')
def api_darshan_queue():
    """Everyone the attendance desk has sent to darshan - the one green tick.

    This is what removes the typing at Guruji's desk: the attendance desk
    scans a person a second time as they go through, and this screen takes the
    first one not yet finished.

    A row leaves the moment Guruji's desk finishes it: the strip is the work
    still to do, and the people already seen are counted on the Darshan
    Completed button beside it, which is where anyone looking for them goes.
    That count comes back here too, so one poll keeps both badges right.

    Chronological by the darshan mark, because that is the order they were
    sent, with the seat number breaking a tie.

    Only seat and name come back. Pledge detail for two hundred people polled
    every few seconds would be waste - /api/darshan/pending and
    /api/darshan/completed carry that, for the lists where it is read.
    """
    year = request.args.get('year', get_current_year(), type=int)
    limit = min(request.args.get('limit', 300, type=int), 500)
    cf = get_user_center_filter()
    q = Attendance.query.filter_by(pujan_year=year).filter(
        Attendance.darshan_at.isnot(None))
    if cf:
        q = q.filter_by(center_id=cf)
    sent = q.order_by(Attendance.darshan_at.asc(),
                      Attendance.seat_no.asc()).all()
    waiting = [a for a in sent if not a.darshan_done_at]
    rows = [{'member_id': a.member_id,
             'member_code': (a.member.member_code or '') if a.member else '',
             'member_name': a.member.display_name if a.member else '',
             'seat_no': a.seat_no, 'seat_label': a.seat_label,
             'darshan': True, 'done': False,
             'marked_time': to_ist(a.darshan_at, '%H:%M') or ''}
            for a in waiting[-limit:]]
    return jsonify({'year': year, 'rows': rows, 'total': len(sent),
                    'waiting': len(waiting),
                    'done': len(sent) - len(waiting)})


def _darshan_detail(rows, year, cf):
    """Seat, pass, name and pledges for a list of attendance rows.

    Shared by the Pending and Darshan Completed lists: both are read on demand
    rather than polled, so both can afford the detail the queue strip leaves
    out. One SQL round trip for the pledges, not one per person.
    """
    mids = [a.member_id for a in rows]
    pledges = {}
    if mids:
        for sk in Sankalp.query.filter(Sankalp.pujan_year == year,
                                       Sankalp.member_id.in_(mids)).all():
            pledges.setdefault(sk.member_id, []).append(sk)
    out = []
    for a in rows:
        mine = pledges.get(a.member_id, [])
        out.append({
            'member_id': a.member_id,
            'seat_no': a.seat_no, 'seat_label': a.seat_label,
            'pass_number': a.pas.pass_number if a.pas else '',
            'member_code': (a.member.member_code or '') if a.member else '',
            'member_name': a.member.display_name if a.member else '',
            'center_name': a.center.label if a.center else '',
            'sankalp_name': ', '.join(sk.sankalp_name for sk in mine) or '\u2014',
            'amount': float(sum(sk.amount for sk in mine)) if mine else 0.0,
            'check_in_time': to_ist(a.check_in_time, '%d-%m-%Y %H:%M') or '',
            'darshan_time': to_ist(a.darshan_at, '%d-%m-%Y %H:%M') or '',
            'darshan_done_time': to_ist(a.darshan_done_at, '%d-%m-%Y %H:%M') or '',
        })
    return strip_money({'year': year, 'rows': out, 'total': len(out),
                        'amount': sum(r['amount'] for r in out),
                        'show_center': not bool(cf)})


@app.route('/api/darshan/pending')
@perm_required('attendance.darshan')
def api_darshan_pending():
    """Came in, but the attendance desk has not sent them to darshan yet.

    No green tick at all: present in the hall, not yet through the darshan
    line. This is the list somebody walks the hall with.
    """
    year = request.args.get('year', get_current_year(), type=int)
    cf = get_user_center_filter()
    q = Attendance.query.filter_by(pujan_year=year).filter(
        Attendance.darshan_at.is_(None))
    if cf:
        q = q.filter_by(center_id=cf)
    waiting = q.order_by(Attendance.check_in_time.asc(),
                         Attendance.seat_no.asc()).all()
    return jsonify(_darshan_detail(waiting, year, cf))


@app.route('/api/darshan/completed')
@perm_required('attendance.darshan')
def api_darshan_completed():
    """Finished at Guruji's desk - the two green ticks.

    Newest first: on a day that is still running, what the desk wants to check
    is the person it just saved, not the first one of the morning.
    """
    year = request.args.get('year', get_current_year(), type=int)
    cf = get_user_center_filter()
    q = Attendance.query.filter_by(pujan_year=year).filter(
        Attendance.darshan_done_at.isnot(None))
    if cf:
        q = q.filter_by(center_id=cf)
    done = q.order_by(Attendance.darshan_done_at.desc(),
                      Attendance.seat_no.asc()).all()
    return jsonify(_darshan_detail(done, year, cf))


@app.route('/api/darshan/lookup')
@perm_required('attendance.darshan')
def api_darshan_lookup():
    """One person's pledges, as the darshan desk needs to see them.

    The same figures the bulk-edit grid shows, plus what they pledged and paid
    last year - which is the question actually asked at the desk, and the
    reason a plain check-in screen was not enough. Last year is matched on the
    firm or company, not on the row id, so a partner who moved centre still
    lines up with their own history.
    """
    year = request.args.get('year', get_current_year(), type=int)
    member, att, err = darshan_target_from_request(request.args, year)
    if err:
        return jsonify({'error': err,
                        'member': member.display_name if member else '',
                        'needs_checkin': bool(member and not att)}), 404
    cf = get_user_center_filter()
    if cf and att.center_id != cf:
        return jsonify({'error': 'That person belongs to another center'}), 403

    prev = {}
    for old in Sankalp.query.filter_by(member_id=member.id,
                                       pujan_year=year - 1).all():
        got = sum(float(c.amount) for c in old.collections)
        prev[old.entity_id or 0] = {'amount': float(old.amount), 'collected': got,
                                    'pending': float(old.amount) - got}

    rows = []
    for sk in Sankalp.query.filter_by(member_id=member.id,
                                      pujan_year=year).order_by(Sankalp.id).all():
        got = sum(float(c.amount) for c in sk.collections)
        last = prev.get(sk.entity_id or 0, {})
        rows.append({
            'id': sk.id,
            'sankalp_type': sk.sankalp_type,
            'sankalp_name': sk.sankalp_name,
            'center_name': sk.center.label if sk.center else '',
            'amount': float(sk.amount), 'collected': got,
            'pending': float(sk.amount) - got,
            'last_amount': last.get('amount', 0.0),
            'last_collected': last.get('collected', 0.0),
            # What was still owed at the end of last year - the figure the desk
            # asks about, where last year's receipts are of no interest now.
            'last_pending': last.get('pending', 0.0),
            'verified_at': to_ist(sk.verified_at, '%d-%m-%Y %H:%M'),
        })

    payload = {
        'year': year,
        'last_year': year - 1,
        'member': {'id': member.id, 'member_code': member.member_code or '',
                   'name': member.display_name,
                   'mobile': member.mobile or '',
                   'center_name': member.center.label if member.center else '',
                   # Dharmada belongs to the member, not the pledge, so it is
                   # one pair of fields however many firms they hold.
                   'dharmada_type': member.dharmada_type or '',
                   'dharmada_amount': float(member.dharmada_amount or 0)},
        'seat_no': att.seat_no, 'seat_label': att.seat_label,
        'darshan': bool(att.darshan_at),
        'darshan_time': to_ist(att.darshan_at, '%d-%m-%Y %H:%M') or '',
        'darshan_done': bool(att.darshan_done_at),
        'darshan_done_time': to_ist(att.darshan_done_at, '%d-%m-%Y %H:%M') or '',
        'pass_number': att.pas.pass_number if att.pas else '',
        'rows': rows,
        'can_edit': has_perm(User.query.get(session['user_id']), 'sankalp.edit'),
        'can_dharmada': can_edit_dharmada(User.query.get(session['user_id'])),
        'totals': {
            'amount': sum(r['amount'] for r in rows),
            'collected': sum(r['collected'] for r in rows),
            'pending': sum(r['pending'] for r in rows),
            'last_amount': sum(r['last_amount'] for r in rows),
            'last_collected': sum(r['last_collected'] for r in rows),
            'last_pending': sum(r['last_pending'] for r in rows),
        },
    }
    return jsonify(strip_money(payload))


@app.route('/api/darshan/save', methods=['POST'])
@perm_required('attendance.darshan')
def api_darshan_save():
    """Any revised amounts, then the darshan tick, in that order.

    One button at the desk, because the two belong together: the amount is
    confirmed with the person standing there, and the tick is what says they
    were seen. If a figure is refused - below what has already been collected,
    say - nothing is stamped, so the row cannot end up ticked with an amount
    that was never saved.
    """
    data = request.get_json(silent=True) or {}
    year = data.get('pujan_year', get_current_year())
    user = User.query.get(session['user_id'])
    member, att, err = darshan_target_from_request(data, year)
    if err:
        return jsonify({'error': err}), 404
    cf = get_user_center_filter()
    if cf and att.center_id != cf:
        return jsonify({'error': 'That person belongs to another center'}), 403

    changes = data.get('amounts') or []
    if changes and not has_perm(user, 'sankalp.edit'):
        return jsonify({'error': 'You may mark darshan but not change amounts. '
                                 'Ask an admin to enable it.'}), 403
    remark = (data.get('remark') or 'Guruji Darshan').strip()
    saved = []
    for item in changes:
        sk = Sankalp.query.get(item.get('id'))
        if not sk or sk.member_id != member.id or sk.pujan_year != year:
            return jsonify({'error': 'That pledge is not this member\'s'}), 400
        if cf and sk.center_id != cf:
            return jsonify({'error': f'{sk.sankalp_name} is collected by '
                                     f'another center'}), 403
        try:
            new_amount = Decimal(str(item.get('amount')))
        except (InvalidOperation, TypeError, ValueError):
            return jsonify({'error': f'{sk.sankalp_name}: that is not an amount'}), 400
        if new_amount == sk.amount:
            continue
        if new_amount <= 0:
            return jsonify({'error': f'{sk.sankalp_name}: amount must be '
                                     f'greater than zero'}), 400
        got = sum(float(c.amount) for c in sk.collections)
        if float(new_amount) < got:
            return jsonify({'error': f'{sk.sankalp_name}: Rs.{got:,.0f} has '
                                     f'already been collected. The pledge '
                                     f'cannot be set below that.'}), 400
        before = snapshot(sk, 'sankalp')
        db.session.add(SankalpChange(sankalp_id=sk.id, old_amount=sk.amount,
                                     new_amount=new_amount, remark=remark,
                                     changed_by=user.id))
        sk.amount = new_amount
        # Stamped like a bulk-edit confirmation, so the grid shows the row as
        # done when it is reopened.
        sk.verified_at = utcnow()
        sk.verified_by = user.id
        db.session.commit()
        audit('update', 'sankalp', sk, old=before, new=snapshot(sk, 'sankalp'),
              label=sk.sankalp_name, year=sk.pujan_year)
        db.session.commit()
        saved.append({'id': sk.id, 'amount': float(sk.amount)})

    # Dharmada sits on the member, so it is one write however many pledges
    # were on screen. Done after the amounts and before the tick, so a refused
    # figure leaves nothing stamped.
    dh_changed = False
    if 'dharmada_type' in data or 'dharmada_amount' in data:
        want_type = (data.get('dharmada_type') or '').strip()
        want_amt = _money(data.get('dharmada_amount'))
        now_type = member.dharmada_type or ''
        now_amt = _money(member.dharmada_amount or 0)
        if want_type != now_type or want_amt != now_amt:
            if not can_edit_dharmada(user):
                return jsonify({'error': 'You may mark darshan but not change '
                                         'dharmada. Ask an admin to enable '
                                         'it.'}), 403
            if want_type and want_type not in ('R', 'I/R', 'P', 'N'):
                return jsonify({'error': 'Dharmada type must be R, I/R, P or N'}), 400
            before = {'dharmada_type': now_type, 'dharmada_amount': float(now_amt)}
            member.dharmada_type = want_type
            member.dharmada_amount = want_amt
            db.session.commit()
            audit('update', 'member', member, old=before, label=member.name,
                  new={'dharmada_type': member.dharmada_type,
                       'dharmada_amount': float(member.dharmada_amount or 0)})
            db.session.commit()
            dh_changed = True

    first = mark_darshan_done(att, user)
    return jsonify({'success': True, 'already': not first, 'saved': saved,
                    'dharmada_saved': dh_changed,
                    'member': member.display_name,
                    'member_code': member.member_code or '',
                    'seat_no': att.seat_no, 'seat_label': att.seat_label})


@app.route('/api/attendance/checkin', methods=['POST'])
@perm_required('attendance.checkin')
def api_checkin():
    data = request.get_json(silent=True) or {}
    raw = (data.get('pass_number') or '').strip()
    year = data.get('pujan_year', get_current_year())
    # Accepts a bare number as well as the full code - see resolve_pass().
    p, err = resolve_pass(raw, year)
    if err:
        return jsonify({'error': err}), 404
    cf = get_user_center_filter()
    if cf and p.center_id != cf:
        return jsonify({'error': 'This pass belongs to another center'}), 403
    if p.is_used:
        seen = to_ist(Attendance.query.filter_by(pass_id=p.id).first().check_in_time, '%H:%M') \
            if Attendance.query.filter_by(pass_id=p.id).first() else ''
        return jsonify({'error': f'Already checked in - {p.member.display_name}' +
                                 (f' at {seen}' if seen else '')}), 400
    # The same person may hold more than one pass; they still attend once.
    already = Attendance.query.filter_by(member_id=p.member_id,
                                         pujan_year=p.pujan_year).first()
    if already:
        p.is_used = True
        db.session.commit()
        seen = to_ist(already.check_in_time, '%H:%M')
        other = already.pas.pass_number if already.pas else ''
        return jsonify({'error': f'Already checked in - {p.member.display_name}'
                                 + (f' on {other}' if other and other != p.pass_number else '')
                                 + (f' at {seen}' if seen else '')}), 400
    p.is_used = True
    # The seat number is handed out here, in order of arrival, so it is fixed
    # the moment someone is checked in. Retried on the unique index in case two
    # desks check people in at the same instant.
    try:
        att = claim_seat(p.member, p, p.pujan_year)
    except IntegrityError:
        return jsonify({'error': 'Several desks checked in at the same moment. '
                                 'Try again.'}), 409
    audit('create', 'attendance', att, label=p.member.display_name,
          new={'seat_no': att.seat_no, 'pass_number': p.pass_number})
    db.session.commit()
    # The confirmation message, if one is set up. Never allowed to fail the
    # check-in itself.
    msg = send_checkin_message(p.member, p.pujan_year)
    return jsonify({'success': True, 'attendance': att.to_dict(),
                    'member': p.member.display_name,
                    'seat_no': att.seat_no, 'seat_label': att.seat_label,
                    'message': msg,
                    'can_print': has_perm(User.query.get(session['user_id']), 'slip.print')})


# ======================== API: REPORTS ========================
@app.route('/api/reports/sankalp-summary')
@perm_required('page.reports')
def api_report_sankalp_summary():
    cf = get_user_center_filter()
    year = request.args.get('year', get_current_year(), type=int)
    q = Sankalp.query.filter_by(pujan_year=year)
    if cf: q = q.filter_by(center_id=cf)
    cid = request.args.get('center_id', type=int)
    if cid: q = q.filter_by(center_id=cid)
    # Pass and seat numbers, so the summary can be read next to the passes.
    all_rows = q.all()
    mids = {sk.member_id for sk in all_rows}
    passes = {p.member_id: p for p in Pass.query.filter(
        Pass.pujan_year == year, Pass.member_id.in_(mids or {0})).all()}
    seats = {a.member_id: a for a in Attendance.query.filter(
        Attendance.pujan_year == year, Attendance.member_id.in_(mids or {0})).all()}
    rows = []
    for sk in all_rows:
        collected = sum(float(c.amount) for c in sk.collections)
        pas = passes.get(sk.member_id)
        att = seats.get(sk.member_id)
        rows.append({'id': sk.id,
            'pass_number': pas.pass_number if pas else '',
            'seat_label': att.seat_label if att else '',
            'member_code': (sk.member.member_code or '') if sk.member else '',
            'member_name': sk.member.display_name if sk.member else '',
            'center_name': sk.center.label if sk.center else '',
            'owner_center_name': sk.owner_center.label if sk.owner_center else '',
            'is_cross_center': bool(sk.owner_center_id and sk.owner_center_id != sk.center_id),
            'entity_name': sk.entity.name if sk.entity else '',
            'person_name': sk.person.name if sk.person else '',
            'sankalp_type': sk.sankalp_type,
            'sankalp_name': sk.sankalp_name, 'amount': float(sk.amount),
            'collected': collected, 'pending': float(sk.amount) - collected})
    ta = sum(r['amount'] for r in rows); tc = sum(r['collected'] for r in rows)
    return jsonify({'rows': rows, 'year': year, 'totals': {'amount': ta, 'collected': tc, 'pending': ta-tc}})

@app.route('/api/reports/year-over-year')
@perm_required('page.reports')
def api_report_year_over_year():
    mid = request.args.get('member_id', type=int)
    if not mid: return jsonify({'error': 'Member ID required'}), 400
    member = Member.query.get_or_404(mid)
    # Carry-forward follows the PRIMARY - the member and the firm/company. The
    # collecting centre can change year to year when a partner moves, and that
    # must not break the history.
    sankalps = Sankalp.query.filter_by(member_id=mid).order_by(Sankalp.pujan_year).all()
    rows, groups = [], {}
    for sk in sankalps:
        collected = sum(float(c.amount) for c in sk.collections)
        row = {'year': sk.pujan_year, 'sankalp_type': sk.sankalp_type,
               'sankalp_name': sk.sankalp_name, 'amount': float(sk.amount),
               'collected': collected, 'pending': float(sk.amount) - collected,
               'entity_id': sk.entity_id,
               'entity_name': sk.entity.name if sk.entity else '',
               'person_name': sk.person.name if sk.person else '',
               'collected_by': sk.center.label if sk.center else '',
               'primary_center': sk.owner_center.label if sk.owner_center else '',
               'is_cross_center': bool(sk.owner_center_id and sk.owner_center_id != sk.center_id)}
        rows.append(row)
        key = sk.entity_id or 0
        groups.setdefault(key, {
            'entity_id': sk.entity_id,
            'label': (sk.entity.name if sk.entity else member.name),
            'years': []})['years'].append(row)
    return jsonify({'member': member.to_dict(), 'years': rows,
                    'by_primary': list(groups.values())})

@app.route('/api/reports/attendance-grid')
@perm_required('page.reports', 'attendance.list')
def api_attendance_grid():
    """Attendance split the way the centres think about it.

    For each centre: total members, then At Center and At Head Office, each
    broken into Expecting / Present / Absent.

    Expecting is everyone on that side - those ticked to travel to Head Office,
    or everyone else at their own centre - including anyone registered at the
    door on the day, who joins the side of the desk that registered them.
    Present and Absent are the same people cut the other way, so Expecting
    equals Present + Absent for each half, and the two halves make the total.

    There is no separate New column: a walk-in is by definition present, so the
    number was always duplicated. Whether a given person was a walk-in shows on
    the list behind the Present figure instead.
    """
    year = request.args.get('year', get_current_year(), type=int)
    cf = get_user_center_filter()
    centers = Center.query.filter_by(is_active=True).order_by(Center.name_en).all()
    if cf:
        centers = [c for c in centers if c.id == cf]

    att_rows = Attendance.query.filter_by(pujan_year=year).all()
    checked = {a.member_id for a in att_rows}
    # Everyone who was seen at darshan. The difference between this and
    # present is the list a centre acts on afterwards.
    darshan = {a.member_id for a in att_rows if a.darshan_at}
    rows, totals = [], {k: 0 for k in (
        'total_members', 'c_expecting', 'c_present', 'c_absent',
        'h_expecting', 'h_present', 'h_absent',
        'present', 'darshan', 'no_darshan')}

    for c in centers:
        members = Member.query.filter_by(center_id=c.id, is_active=True).all()
        r = {'center_id': c.id, 'center_name': c.label, 'total_members': len(members),
             'c_expecting': 0, 'c_present': 0, 'c_absent': 0,
             'h_expecting': 0, 'h_present': 0, 'h_absent': 0,
             'present': 0, 'darshan': 0, 'no_darshan': 0}
        for m in members:
            side = 'h' if m.attend_at_ho else 'c'
            r[f'{side}_expecting'] += 1
            r[f'{side}_present' if m.id in checked else f'{side}_absent'] += 1
            if m.id in checked:
                r['present'] += 1
                if m.id in darshan:
                    r['darshan'] += 1
                else:
                    r['no_darshan'] += 1
        rows.append(r)
        for k in totals:
            totals[k] += r[k]

    return jsonify({'year': year, 'rows': rows, 'totals': totals,
                    'show_center': not bool(cf)})


def member_is_new(m, year):
    """Whether this member was registered at the door on the day.

    Deliberately not "created during this year": members are imported and added
    all through the drive, and counting those as New would put almost everyone
    in that column. New means they walked up on the day and were entered
    through Fast Entry with "Today is the event date" ticked.
    """
    return m.event_registered_year == year


@app.route('/api/reports/attendance-detail')
@perm_required('page.reports', 'attendance.list')
def api_attendance_detail():
    """The people behind one number in the attendance report.

    `status` is present or absent, `center_id` the row that was clicked. Money
    goes through the usual stripping, so a user without the right sees the
    names but not the figures.
    """
    year = request.args.get('year', get_current_year(), type=int)
    status = (request.args.get('status') or 'absent').lower()
    # Which half of the grid, and which of its four numbers.
    side = (request.args.get('side') or '').lower()      # center | ho
    cid = request.args.get('center_id', type=int)
    cf = get_user_center_filter()
    if cf:
        cid = cf                      # a centre user only ever sees their own

    q = Member.query.filter_by(is_active=True)
    if cid:
        q = q.filter_by(center_id=cid)
    members = q.order_by(Member.name).all()
    if side == 'ho':
        members = [m for m in members if m.attend_at_ho]
    elif side == 'center':
        members = [m for m in members if not m.attend_at_ho]

    checked = {a.member_id: a for a in Attendance.query.filter_by(pujan_year=year).all()}
    rows = []
    for m in members:
        here = m.id in checked
        seen = bool(here and checked[m.id].darshan_at)
        if status == 'present' and not here:
            continue
        if status == 'absent' and here:
            continue
        # Guruji Darshan is counted among those who came: somebody who never
        # arrived is absent, not "missed darshan", and mixing the two would
        # make the difference figure useless.
        if status == 'darshan' and not seen:
            continue
        if status == 'no_darshan' and (not here or seen):
            continue
        if status == 'new' and not member_is_new(m, year):
            continue
        # Expecting is now everyone on that side, walk-ins included, so it
        # filters on nothing beyond the side itself. 'new' and 'existing' are
        # still accepted for anything that asks for them directly.
        if status == 'existing' and member_is_new(m, year):
            continue
        sks = Sankalp.query.filter_by(member_id=m.id, pujan_year=year).all()
        total = sum(Decimal(str(x.amount)) for x in sks) if sks else Decimal('0')
        got = (sum(sum(Decimal(str(c.amount)) for c in x.collections) for x in sks)
               if sks else Decimal('0'))
        att = checked.get(m.id)
        rows.append({
            'member_id': m.id,
            'member_code': m.member_code or '',
            'member_name': m.name,
            'center_name': m.center.label if m.center else '',
            'sankalp_name': ', '.join(x.sankalp_name for x in sks) or '—',
            'amount': float(total), 'collected': float(got),
            'pending': float(total - got),
            'seat_label': att.seat_label if att else '',
            'darshan': bool(att.darshan_at) if att else False,
            'darshan_time': (to_ist(att.darshan_at, '%d-%m-%Y %H:%M')
                             if att and att.darshan_at else ''),
            'is_new': member_is_new(m, year),
            'at_ho': bool(m.attend_at_ho),
            'checked_in': to_ist(att.check_in_time, '%d-%m-%Y %H:%M') if att else '',
        })
    payload = {'year': year, 'status': status, 'rows': rows, 'total': len(rows),
               'amount': sum(r['amount'] for r in rows),
               'pending': sum(r['pending'] for r in rows),
               # A centre user has one centre, so the column is noise for them.
               'show_center': not bool(cf)}
    return jsonify(strip_money(payload))


@app.route('/api/reports/attendance-report')
@perm_required('page.reports', 'page.attendance')
def api_report_attendance():
    """Per-person rows plus a centre-wise present/absent summary.

    "Expected" is the number of passes issued for that centre, which is what a
    centre actually plans for; absent is expected minus present. Members sent
    to Head Office are counted separately so a centre is not marked absent for
    people it deliberately sent away.
    """
    year = request.args.get('year', get_current_year(), type=int)
    cf = get_user_center_filter()

    q = Attendance.query.filter_by(pujan_year=year)
    if cf:
        q = q.filter_by(center_id=cf)
    rows = [a.to_dict() for a in q.order_by(Attendance.check_in_time.desc()).all()]

    qc = Center.query.filter_by(is_active=True)
    if cf:
        qc = qc.filter_by(id=cf)
    summary = []
    for c in qc.order_by(Center.name_en).all():
        members = Member.query.filter_by(center_id=c.id, is_active=True).count()
        expected = Pass.query.filter_by(center_id=c.id, pujan_year=year).count()
        present = db.session.query(db.func.count(db.distinct(Attendance.member_id))).filter(
            Attendance.center_id == c.id, Attendance.pujan_year == year).scalar() or 0
        at_ho = Member.query.filter_by(center_id=c.id, is_active=True,
                                       attend_at_ho=True).count()
        summary.append({
            'center_id': c.id, 'center_name': c.label,
            'total_members': members,
            'expected': expected,
            'present': present,
            'absent': max(expected - present, 0),
            'at_ho': at_ho,
            'percent': round(present * 100.0 / expected, 1) if expected else 0.0,
        })
    totals = {
        'total_members': sum(r['total_members'] for r in summary),
        'expected': sum(r['expected'] for r in summary),
        'present': sum(r['present'] for r in summary),
        'absent': sum(r['absent'] for r in summary),
        'at_ho': sum(r['at_ho'] for r in summary),
    }
    totals['percent'] = (round(totals['present'] * 100.0 / totals['expected'], 1)
                         if totals['expected'] else 0.0)
    # Everyone who has not come. Sitting next to the check-in list is the
    # point: the one you act on is the shorter one.
    checked = {a.member_id for a in Attendance.query.filter_by(pujan_year=year).all()}
    mq = Member.query.filter_by(is_active=True)
    if cf:
        mq = mq.filter_by(center_id=cf)
    absent = []
    for m in mq.order_by(Member.name).all():
        if m.id in checked:
            continue
        p = Pass.query.filter_by(member_id=m.id, pujan_year=year).first()
        absent.append({
            'member_id': m.id, 'member_code': m.member_code or '',
            'member_name': m.name,
            'center_name': m.center.label if m.center else '',
            'mobile': m.mobile or '',
            'pass_number': p.pass_number if p else '',
            'at_ho': bool(m.attend_at_ho),
            'is_new': member_is_new(m, year),
        })
    return jsonify({'year': year, 'rows': rows, 'total': len(rows),
                    'absent': absent, 'absent_total': len(absent),
                    'summary': summary, 'totals': totals})


@app.route('/api/reports/center-summary')
@perm_required('page.reports')
def api_report_center_summary():
    year = request.args.get('year', get_current_year(), type=int)
    cf = get_user_center_filter()
    rows = []
    q = Center.query.filter_by(is_active=True)
    if cf:                       # was listing every centre regardless of login
        q = q.filter_by(id=cf)
    for c in q.all():
        sks = Sankalp.query.filter_by(center_id=c.id, pujan_year=year).all()
        amt = sum(float(s.amount) for s in sks)
        coll = sum(float(co.amount) for s in sks for co in s.collections)
        passes = Pass.query.filter_by(center_id=c.id, pujan_year=year).count()
        # Distinct members, so one person cannot be counted twice.
        att = db.session.query(db.func.count(db.distinct(Attendance.member_id))).filter(
            Attendance.center_id == c.id, Attendance.pujan_year == year).scalar() or 0
        rows.append({'center_id': c.id, 'center_name': c.label,
            'members': c.members.filter_by(is_active=True).count(),
            'sankalps': len(sks), 'amount': amt, 'collected': coll,
            'pending': amt - coll, 'passes': passes, 'attendance': att})
    totals = {k: sum(r[k] for r in rows) for k in
              ('members', 'sankalps', 'amount', 'collected', 'pending', 'passes', 'attendance')}
    return jsonify({'year': year, 'rows': rows, 'totals': totals})


# ======================== API: USERS ========================
SYSTEM_ROLES = [
    ('super_admin', 'Super Admin', 'સુપર એડમિન', False, 10),
    ('admin', 'Admin', 'એડમિન', False, 20),
    ('center_sant', 'Center Sant', 'સેન્ટર સંત', True, 30),
    ('center_user', 'Center User', 'સેન્ટર યુઝર', True, 40),
]


def seed_roles():
    """Puts the four built-in roles in the table so they can be edited like any
    other. Existing rows are left alone."""
    made = 0
    for key, name, name_gu, scoped, order in SYSTEM_ROLES:
        if Role.query.filter_by(key=key).first():
            continue
        db.session.add(Role(
            key=key, name=name, name_gu=name_gu, center_scoped=scoped,
            is_system=True, sort_order=order,
            permissions=json.dumps(ROLE_PERMISSIONS.get(key, {}))))
        made += 1
    if made:
        db.session.commit()
    return made


def backfill_role_permissions():
    """Gives the built-in roles any permission key added since they were seeded.

    A Role row stores its own map, and `perm_map` reads a missing key as off.
    So a key introduced by a new build - attendance.darshan, for one - would be
    off for Center Sant on an existing installation even though the shipped
    default has it on, and nobody would know to look for it. Only keys entirely
    absent from the saved map are filled in, so a deliberate change an admin
    made is never undone. Roles the admin created are left alone: a new right
    on a custom role is their decision, not ours.
    """
    fixed = 0
    for r in Role.query.filter_by(is_system=True).all():
        try:
            saved = json.loads(r.permissions) if r.permissions else {}
        except (ValueError, TypeError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        defaults = ROLE_PERMISSIONS.get(r.key, {})
        added = [k for k in ALL_PERMISSIONS if k not in saved]
        if not added:
            continue
        for k in added:
            saved[k] = bool(defaults.get(k, False))
        r.permissions = json.dumps(saved)
        fixed += 1
    if fixed:
        db.session.commit()
        print('[init] Added new permission keys to %d built-in role(s)' % fixed)
    return fixed


@app.route('/api/roles')
@login_required
def api_get_roles():
    """Every user needs this to render role names; only an admin sees the
    permission detail."""
    me = User.query.get(session['user_id'])
    detail = has_perm(me, 'user.manage')
    rows = Role.query.filter_by(is_active=True).order_by(Role.sort_order, Role.name).all()
    return jsonify([r.to_dict(with_counts=detail) if detail
                    else {'key': r.key, 'name': r.name, 'name_gu': r.name_gu or '',
                          'center_scoped': r.center_scoped}
                    for r in rows])


def _role_key_from(name, given=''):
    """A stable machine key from the display name: 'Center Accountant' ->
    'center_accountant'. Users store the key, so it must never change."""
    raw = (given or name or '').strip().lower()
    key = re.sub(r'[^a-z0-9]+', '_', raw).strip('_')[:40]
    if not key:
        key = 'role'
    base, n = key, 1
    while Role.query.filter_by(key=key).first():
        n += 1
        key = f'{base}_{n}'[:40]
    return key


@app.route('/api/roles', methods=['POST'])
@perm_required('user.manage')
def api_create_role():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Give the role a name'}), 400
    me = User.query.get(session['user_id'])
    mine = effective_permissions(me)
    wanted = {k: bool(v) for k, v in (data.get('permissions') or {}).items()
              if k in ALL_PERMISSIONS}
    # Nobody can create a role more powerful than themselves.
    denied = [k for k, v in wanted.items() if v and not mine.get(k)]
    if denied:
        return jsonify({'error': 'You cannot grant rights you do not have yourself: '
                                 + ', '.join(denied[:4])}), 403
    r = Role(key=_role_key_from(name, data.get('key')), name=name,
             name_gu=(data.get('name_gu') or '').strip(),
             description=(data.get('description') or '').strip(),
             center_scoped=bool(data.get('center_scoped', True)),
             is_system=False, sort_order=int(data.get('sort_order') or 100),
             permissions=json.dumps(wanted))
    db.session.add(r); db.session.commit()
    audit('create', 'role', r, label=name, new={'key': r.key, 'granted': r.to_dict()['granted']})
    db.session.commit()
    return jsonify({'success': True, 'role': r.to_dict(with_counts=True)})


@app.route('/api/roles/<int:rid>', methods=['PUT'])
@perm_required('user.manage')
def api_update_role(rid):
    data = request.get_json(silent=True) or {}
    r = Role.query.get_or_404(rid)
    me = User.query.get(session['user_id'])
    if r.key == 'super_admin' and not is_super(me):
        return jsonify({'error': 'Only a super admin can change the Super Admin role'}), 403
    before = {'name': r.name, 'center_scoped': r.center_scoped, 'permissions': r.permissions}
    mine = effective_permissions(me)
    if 'name' in data and (data.get('name') or '').strip():
        r.name = data['name'].strip()
    for f in ('name_gu', 'description'):
        if f in data:
            setattr(r, f, (data.get(f) or '').strip())
    if 'center_scoped' in data and not r.is_system:
        r.center_scoped = bool(data['center_scoped'])
    if 'is_active' in data and not r.is_system:
        r.is_active = bool(data['is_active'])
    if 'sort_order' in data:
        r.sort_order = int(data['sort_order'] or 100)
    if 'permissions' in data:
        if r.key == 'super_admin':
            return jsonify({'error': 'The Super Admin role always holds every right'}), 400
        current = r.perm_map
        wanted = {k: bool(v) for k, v in (data['permissions'] or {}).items()
                  if k in ALL_PERMISSIONS}
        denied = [k for k, v in wanted.items()
                  if v and not current.get(k) and not mine.get(k)]
        if denied:
            return jsonify({'error': 'You cannot grant rights you do not have yourself: '
                                     + ', '.join(denied[:4])}), 403
        r.permissions = json.dumps(wanted)
    db.session.commit()
    audit('update', 'role', r, old=before,
          new={'name': r.name, 'center_scoped': r.center_scoped, 'permissions': r.permissions},
          label=r.name)
    db.session.commit()
    return jsonify({'success': True, 'role': r.to_dict(with_counts=True)})


@app.route('/api/roles/<int:rid>', methods=['DELETE'])
@perm_required('user.manage')
def api_delete_role(rid):
    r = Role.query.get_or_404(rid)
    if r.is_system:
        return jsonify({'error': 'The built-in roles cannot be deleted. '
                                 'You can edit their rights instead.'}), 400
    n = User.query.filter_by(role=r.key).count()
    if n:
        return jsonify({'error': f'{n} user(s) still have this role. '
                                 f'Move them to another role first.'}), 400
    audit('delete', 'role', r, old={'key': r.key, 'name': r.name}, label=r.name)
    db.session.delete(r); db.session.commit()
    return jsonify({'success': True})


@app.route('/api/permissions')
@perm_required('user.manage')
def api_permissions_catalog():
    """The full list, grouped for the tick-box screen, plus each role's
    defaults so the UI can show what is inherited."""
    return jsonify({
        'groups': [{'key': g, 'label': lbl,
                    'items': [{'key': k, 'label': t} for k, t in items]}
                   for g, lbl, items in PERMISSION_GROUPS],
        'role_defaults': ROLE_PERMISSIONS,
    })


@app.route('/api/users/<int:uid>/permissions', methods=['GET'])
@perm_required('user.manage')
def api_get_user_permissions(uid):
    u = User.query.get_or_404(uid)
    try:
        overrides = json.loads(u.permissions) if u.permissions else {}
    except (ValueError, TypeError):
        overrides = {}
    return jsonify({'user_id': u.id, 'username': u.username, 'role': u.role,
                    'effective': effective_permissions(u),
                    'overrides': overrides,
                    'role_defaults': ROLE_PERMISSIONS.get(u.role, {})})


@app.route('/api/users/<int:uid>/permissions', methods=['PUT'])
@perm_required('user.manage')
def api_set_user_permissions(uid):
    """Stores only the differences from the role defaults, so changing someone's
    role later still moves their baseline with it."""
    data = request.get_json(silent=True) or {}
    u = User.query.get_or_404(uid)
    me = User.query.get(session['user_id'])
    if u.id == me.id and me.role == 'super_admin':
        return jsonify({'error': 'You cannot change your own permissions'}), 400
    if u.role == 'super_admin' and me.role != 'super_admin':
        return jsonify({'error': 'Only a super admin can change a super admin'}), 403

    before = {'permissions': u.permissions}
    if data.get('reset'):
        u.permissions = None
    else:
        wanted = data.get('permissions') or {}
        defaults = ROLE_PERMISSIONS.get(u.role, {})
        overrides = {k: bool(v) for k, v in wanted.items()
                     if k in defaults and bool(v) != defaults[k]}
        # An admin can never grant a permission they do not hold themselves.
        if me.role != 'super_admin':
            mine = effective_permissions(me)
            overrides = {k: v for k, v in overrides.items() if not (v and not mine.get(k))}
        u.permissions = json.dumps(overrides) if overrides else None
    db.session.commit()
    audit('update', 'user', u, old=before, new={'permissions': u.permissions},
          label=u.full_name or u.username)
    db.session.commit()
    return jsonify({'success': True, 'effective': effective_permissions(u),
                    'overrides': json.loads(u.permissions) if u.permissions else {}})


@app.route('/api/users', methods=['GET'])
@perm_required('user.manage')
def api_get_users():
    return jsonify([u.to_dict() for u in User.query.order_by(User.role, User.full_name).all()])

@app.route('/api/users', methods=['POST'])
@perm_required('user.manage')
def api_create_user():
    """The role must exist, and a centre-scoped role must name a centre."""
    data = request.get_json()
    if User.query.filter_by(username=data['username']).first():
        return jsonify({'error': 'Username already exists'}), 400
    role = get_role(data.get('role'))
    if not role:
        return jsonify({'error': f'Unknown role "{data.get("role")}"'}), 400
    utype = (data.get('user_type') or default_user_type(data.get('role'))).strip()
    if utype not in ('team_member', 'center'):
        return jsonify({'error': 'User type must be Team Member or Center'}), 400
    cid = data.get('center_id') or None
    if utype == 'center' and not cid:
        return jsonify({'error': 'A center user needs a center'}), 400
    if utype == 'team_member':
        cid = None                       # works across every centre
    u = User(username=data['username'], full_name=data['full_name'], role=data['role'],
             user_type=utype, center_id=cid,
             mobile=data.get('mobile', ''), email=data.get('email', ''))
    pw = data.get('password') or ''
    problem = password_problem(pw, u.username)
    if problem:
        return jsonify({'error': f'The password you set for them: {problem.lower()}'}), 400
    u.set_password(pw)
    # A password you chose for somebody else is not theirs yet - you know it.
    # They pick their own the first time they sign in.
    u.must_change_password = True
    db.session.add(u); db.session.commit()
    audit('create', 'user', u, label=u.full_name,
          new={'username': u.username, 'role': u.role})
    db.session.commit()
    return jsonify({'success': True, 'user': u.to_dict()})

@app.route('/api/users/<int:uid>', methods=['GET'])
@perm_required('user.manage')
def api_get_user(uid):
    """Third instance of the same missing-route bug: Edit on the Users list
    was hitting a 405."""
    return jsonify(User.query.get_or_404(uid).to_dict())


@app.route('/api/users/<int:uid>', methods=['PUT'])
@perm_required('user.manage')
def api_update_user(uid):
    data = request.get_json()
    u = User.query.get_or_404(uid)
    before = snapshot(u, 'user')
    if 'role' in data:
        if not get_role(data['role']):
            return jsonify({'error': f'Unknown role "{data["role"]}"'}), 400
        u.role = data['role']
    utype = (data.get('user_type') or u.user_type or default_user_type(u.role))
    if utype not in ('team_member', 'center'):
        return jsonify({'error': 'User type must be Team Member or Center'}), 400
    cid = data.get('center_id', u.center_id) or None
    if utype == 'center' and not cid:
        return jsonify({'error': 'A center user needs a center'}), 400
    u.user_type = utype
    u.center_id = None if utype == 'team_member' else cid
    u.full_name = data.get('full_name', u.full_name)
    u.mobile = data.get('mobile', u.mobile)
    u.email = data.get('email', u.email)
    u.is_active = data.get('is_active', u.is_active)
    if data.get('password'):
        problem = password_problem(data['password'], u.username)
        if problem:
            return jsonify({'error': f'The password you set for them: '
                                     f'{problem.lower()}'}), 400
        u.set_password(data['password'])
        # A reset is a temporary password by definition.
        if u.id != session.get('user_id'):
            u.must_change_password = True
    db.session.commit()
    audit('update', 'user', u, old=before, new=snapshot(u, 'user'),
          label=u.full_name or u.username)
    db.session.commit()
    return jsonify({'success': True, 'user': u.to_dict()})

@app.route('/api/users/<int:uid>', methods=['DELETE'])
@perm_required('user.manage')
def api_delete_user(uid):
    """A user is referenced by everything they ever recorded, so the row cannot
    just be deleted - Postgres refuses on the foreign keys, which is the error
    you saw. Those references are informational (who entered this sankalp), so
    they are cleared and the history is kept.
    """
    if uid == session.get('user_id'):
        return jsonify({'error': 'You cannot delete your own account'}), 400
    u = User.query.get_or_404(uid)
    me = User.query.get(session['user_id'])
    if u.role == 'super_admin' and not is_super(me):
        return jsonify({'error': 'Only a super admin can remove a super admin'}), 403
    if u.role == 'super_admin' and User.query.filter_by(role='super_admin', is_active=True).count() <= 1:
        return jsonify({'error': 'This is the only super admin. Create another one first.'}), 400

    label = u.full_name or u.username
    audit('delete', 'user', u, old=snapshot(u, 'user'), label=label)

    # Detach the informational references, keeping the records themselves.
    for model, field in ((Sankalp, 'created_by'), (Collection, 'created_by'),
                         (Pass, 'created_by'), (Attendance, 'created_by'),
                         (Member, 'ho_marked_by'), (PassConfig, 'updated_by'),
                         (SankalpChange, 'changed_by'), (AuditLog, 'user_id')):
        if hasattr(model, field):
            try:
                model.query.filter(getattr(model, field) == uid).update(
                    {field: None}, synchronize_session=False)
            except Exception as e:
                print(f"[delete user] {model.__tablename__}.{field}: {e.__class__.__name__}")
    db.session.delete(u)
    db.session.commit()
    return jsonify({'success': True})


# ======================== EVENT CONFIG & NOTIFICATIONS ========================
def get_event_config(year):
    cfg = EventConfig.query.filter_by(pujan_year=year).first()
    if not cfg:
        cfg = EventConfig(pujan_year=year)
        db.session.add(cfg)
        db.session.commit()
    return cfg


# ---------------------------------------------------------------------------
# What a member may change about themselves
#
# Reached from a signed link in a message, with no login - members do not have
# one. So the surface is kept as small as it can usefully be: the names of
# their own firms, and adding one. Nothing about money, nothing about seats or
# passes, nothing about anybody else. Amounts are confirmed face to face at
# Guruji's desk, which is the right place for them.
# ---------------------------------------------------------------------------
SELFSERVE_MAX_FIRMS = 12


def _selfserve_load(token):
    """(member, cfg, error) for a token, or (None, None, reason)."""
    mid = selfserve_member_id(token)
    if mid is None:
        return None, None, 'bad_link'
    member = db.session.get(Member, mid)
    if not member or not member.is_active:
        return None, None, 'bad_link'
    cfg = get_event_config(get_current_year())
    live, why = selfserve_state(cfg)
    if not live:
        return member, cfg, why
    return member, cfg, ''


@app.route('/my/<token>')
def selfserve_page(token):
    """The member's own page. Deliberately its own template, not the app."""
    member, cfg, err = _selfserve_load(token)
    if err == 'bad_link':
        return render_template('selfserve.html', error='bad_link',
                               lang=(request.args.get('lang')
                                     if request.args.get('lang') in ('en', 'gu')
                                     else DEFAULT_LANGUAGE),
                               year=get_current_year()), 404
    # The interface language, not the language the messages went out in. A
    # Gujarati message can perfectly well point at a page somebody reads in
    # English, and the switch in the corner is there either way.
    lang = request.args.get('lang') or DEFAULT_LANGUAGE
    if lang not in ('en', 'gu'):
        lang = 'en'
    return render_template('selfserve.html', error=err, lang=lang,
                           token=token, member=member,
                           entities=list(member.entities),
                           year=get_current_year(),
                           until=(cfg.selfserve_until if cfg else None),
                           contact=(cfg.contact_phone if cfg else ''))


@app.route('/my/<token>', methods=['POST'])
@csrf.exempt
def selfserve_save(token):
    """Takes the member's corrections.

    Rate limited on the token, because this is a public write path: a
    forwarded link should not become a way to rewrite a record repeatedly.
    Every submission is recorded whether or not it is applied at once.
    """
    member, cfg, err = _selfserve_load(token)
    if err:
        return jsonify({'error': err}), 403 if err != 'bad_link' else 404
    if not rate_ok(f'selfserve:{member.id}', limit=20, minutes=10):
        return jsonify({'error': 'too_many'}), 429

    data = request.get_json(silent=True) or {}
    year = get_current_year()
    ip = (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
          or request.remote_addr or '')
    made, applied = [], 0

    # Renames, one per firm the member already holds.
    for row in (data.get('edits') or []):
        ent = db.session.get(Entity, row.get('id') or 0)
        if not ent or ent.member_id != member.id:
            continue                      # not theirs; silently ignored
        name = (row.get('name') or '').strip()[:200]
        if not name or name == (ent.name or ''):
            continue
        req = MemberRequest(member_id=member.id, pujan_year=year,
                            kind='edit_firm', entity_id=ent.id,
                            entity_type=ent.entity_type,
                            old_name=ent.name or '', new_name=name,
                            from_ip=ip)
        if cfg and cfg.selfserve_auto_edit:
            ent.name = name
            req.status, req.auto = 'applied', True
            req.decided_at = utcnow()
            applied += 1
        db.session.add(req)
        made.append(req)

    # Their email address. Offered because a member with none on record can
    # never be sent anything - and asking them for it in the message they
    # cannot receive is not an option. Validated loosely: an address that is
    # obviously not one is refused, and anything past that is proved only by
    # a message arriving.
    want_email = (data.get('email') or '').strip()[:200]
    if want_email and want_email.lower() != (member.email or '').lower():
        if not re.match(r'^[^@\s]+@[^@\s.]+\.[^@\s]+$', want_email):
            return jsonify({'error': 'bad_email'}), 400
        req = MemberRequest(member_id=member.id, pujan_year=year,
                            kind='set_email', old_name=member.email or '',
                            new_name=want_email, from_ip=ip)
        if cfg and cfg.selfserve_auto_email:
            member.email = want_email
            req.status, req.auto = 'applied', True
            req.decided_at = utcnow()
            applied += 1
        db.session.add(req)
        made.append(req)

    # And any new ones.
    for row in (data.get('adds') or []):
        name = (row.get('name') or '').strip()[:200]
        if not name:
            continue
        if len(list(member.entities)) >= SELFSERVE_MAX_FIRMS:
            break
        etype = 'company' if (row.get('entity_type') == 'company') else 'firm'
        req = MemberRequest(member_id=member.id, pujan_year=year,
                            kind='add_firm', entity_type=etype,
                            new_name=name, from_ip=ip)
        if cfg and cfg.selfserve_auto_add:
            ent = Entity(member_id=member.id, entity_type=etype, name=name)
            db.session.add(ent)
            db.session.flush()
            req.entity_id = ent.id
            req.status, req.auto = 'applied', True
            req.decided_at = utcnow()
            applied += 1
        db.session.add(req)
        made.append(req)

    if not made:
        return jsonify({'success': True, 'saved': 0, 'pending': 0})
    db.session.commit()
    for req in made:
        audit('update' if req.kind == 'edit_firm' else 'create',
              'member_request', req, label=member.display_name,
              new={'kind': req.kind, 'old': req.old_name,
                   'new': req.new_name, 'status': req.status,
                   'by': 'member self-service', 'ip': ip},
              year=year)
    db.session.commit()
    return jsonify({'success': True, 'saved': applied,
                    'pending': len(made) - applied})


DECISION_WORDING = {
    'approved': {
        'en': ('SMVS Chopda-Pujan: your change has been approved. '
               '{what} is now recorded as "{new}". Jay Swaminarayan.'),
        'gu': ('SMVS \u0a9a\u0acb\u0aaa\u0aa1\u0abe \u0aaa\u0ac2\u0a9c\u0aa8: '
               '\u0a86\u0aaa\u0aa8\u0acb \u0aab\u0ac7\u0ab0\u0aab\u0abe\u0ab0 '
               '\u0aae\u0a82\u0a9c\u0ac2\u0ab0 \u0a95\u0ab0\u0ab5\u0abe\u0aae\u0abe\u0a82 '
               '\u0a86\u0ab5\u0acd\u0aaf\u0acb \u0a9b\u0ac7. {what} \u0ab9\u0ab5\u0ac7 '
               '"{new}" \u0aa8\u0acb\u0a82\u0aa7\u0abe\u0aaf\u0ac7\u0ab2 \u0a9b\u0ac7. '
               '\u0a9c\u0aaf \u0ab8\u0acd\u0ab5\u0abe\u0aae\u0abf\u0aa8\u0abe\u0ab0\u0abe\u0aaf\u0aa3.'),
    },
    'rejected': {
        'en': ('SMVS Chopda-Pujan: your requested change to {what} could not '
               'be accepted. Please ring {contact} if it is still needed. '
               'Jay Swaminarayan.'),
        'gu': ('SMVS \u0a9a\u0acb\u0aaa\u0aa1\u0abe \u0aaa\u0ac2\u0a9c\u0aa8: '
               '{what} \u0aae\u0abe\u0a9f\u0ac7 \u0a86\u0aaa\u0ac7 '
               '\u0aae\u0abe\u0a97\u0ac7\u0ab2 \u0aab\u0ac7\u0ab0\u0aab\u0abe\u0ab0 '
               '\u0ab8\u0acd\u0ab5\u0ac0\u0a95\u0abe\u0ab0\u0abe\u0aaf\u0acb '
               '\u0aa8\u0aa5\u0ac0. \u0a9c\u0ab0\u0ac2\u0ab0 \u0ab9\u0acb\u0aaf '
               '\u0aa4\u0acb {contact} \u0a89\u0aaa\u0ab0 \u0ab8\u0a82\u0aaa\u0ab0\u0acd\u0a95 '
               '\u0a95\u0ab0\u0acb. \u0a9c\u0aaf '
               '\u0ab8\u0acd\u0ab5\u0abe\u0aae\u0abf\u0aa8\u0abe\u0ab0\u0abe\u0aaf\u0aa3.'),
    },
}

WHAT_WORDING = {
    'set_email': {'en': 'Your email address',
                  'gu': '\u0a86\u0aaa\u0aa8\u0ac1\u0a82 \u0a88\u0aae\u0ac7\u0ab2'},
    'edit_firm': {'en': 'The firm name',
                  'gu': '\u0aaa\u0ac7\u0aa2\u0ac0\u0aa8\u0ac1\u0a82 \u0aa8\u0abe\u0aae'},
    'add_firm': {'en': 'The new firm',
                 'gu': '\u0aa8\u0ab5\u0ac0 \u0aaa\u0ac7\u0aa2\u0ac0'},
}


def tell_member_decision(req, decision):
    """Tells the member what was decided about their own request.

    Somebody who corrected their firm name and heard nothing back has no way
    of knowing whether it was seen, and a rejection is invisible - the old
    name simply stays. So both outcomes are sent.

    Email if there is an address, and one mobile channel: WhatsApp where it is
    configured, otherwise SMS. Not both, because two messages saying the same
    thing is a cost and an annoyance rather than twice the reassurance.
    """
    member = req.member
    if not member:
        return []
    cfg = get_event_config(req.pujan_year)
    lang = (cfg.language if cfg and cfg.language else DEFAULT_LANGUAGE)
    what = WHAT_WORDING.get(req.kind, WHAT_WORDING['edit_firm'])
    body = DECISION_WORDING[decision][lang].format(
        what=(what.get(lang) or what['en']),
        new=req.new_name or '',
        contact=(cfg.contact_phone if cfg else '') or '')
    sent = []

    if member.email:
        log = CommLog(member_id=member.id, comm_type='email',
                      destination=member.email, pujan_year=req.pujan_year,
                      status='queued', message=body,
                      subject='SMVS Chopda-Pujan')
        db.session.add(log)
        db.session.commit()
        res = notify.send_emails([{'to': member.email, 'body': body,
                                   'subject': 'SMVS Chopda-Pujan',
                                   'ref': log.id}])[0]
        log.status = 'sent' if res.get('ok') else 'failed'
        log.error = res.get('error') or ''
        sent.append(('email', log.status))

    if member.mobile:
        channel = 'whatsapp' if notify.whatsapp_configured() else 'sms'
        log = CommLog(member_id=member.id, comm_type=channel,
                      destination=member.mobile, pujan_year=req.pujan_year,
                      status='queued', message=body)
        db.session.add(log)
        db.session.commit()
        item = {'phone': member.mobile, 'message': body, 'ref': log.id}
        try:
            if channel == 'whatsapp':
                res = notify.send_whatsapp_via_provider([item])[0]
            else:
                res = notify.send_sms_via_provider([item])[0]
        except Exception as e:
            res = {'ok': False, 'error': f'{e.__class__.__name__}: {e}'}
        log.status = 'sent' if res.get('ok') else 'failed'
        log.error = res.get('error') or ''
        sent.append((channel, log.status))

    db.session.commit()
    return sent


@app.route('/api/member-requests')
@perm_required('member.requests')
def api_member_requests():
    """What members have asked for. Pending first, newest first within that."""
    cf = get_user_center_filter()
    status = request.args.get('status') or 'pending'
    q = MemberRequest.query.join(Member, MemberRequest.member_id == Member.id)
    if cf:
        q = q.filter(Member.center_id == cf)
    if status != 'all':
        q = q.filter(MemberRequest.status == status)
    rows = q.order_by(MemberRequest.created_at.desc()).limit(400).all()
    pending = MemberRequest.query.join(
        Member, MemberRequest.member_id == Member.id)
    if cf:
        pending = pending.filter(Member.center_id == cf)
    return jsonify({'rows': [r.to_dict() for r in rows],
                    'pending': pending.filter(
                        MemberRequest.status == 'pending').count()})


@app.route('/api/member-requests/<int:rid>', methods=['POST'])
@perm_required('member.requests')
def api_decide_member_request(rid):
    """Approve or reject one. Approving is what writes it."""
    req = MemberRequest.query.get_or_404(rid)
    cf = get_user_center_filter()
    if cf and req.member and req.member.center_id != cf:
        return jsonify({'error': 'That member belongs to another center'}), 403
    if req.status != 'pending':
        return jsonify({'error': f'That request was already {req.status}'}), 400
    decision = (request.get_json(silent=True) or {}).get('decision')
    if decision not in ('approve', 'reject'):
        return jsonify({'error': 'approve or reject'}), 400

    if decision == 'approve':
        if req.kind == 'set_email':
            req.member.email = req.new_name
        elif req.kind == 'edit_firm':
            ent = db.session.get(Entity, req.entity_id or 0)
            if not ent or ent.member_id != req.member_id:
                return jsonify({'error': 'That firm is no longer there'}), 400
            ent.name = req.new_name
        else:
            ent = Entity(member_id=req.member_id,
                         entity_type=req.entity_type or 'firm',
                         name=req.new_name)
            db.session.add(ent)
            db.session.flush()
            req.entity_id = ent.id
        req.status = 'applied'
    else:
        req.status = 'rejected'
    req.decided_at = utcnow()
    req.decided_by = session['user_id']
    db.session.commit()
    audit('update', 'member_request', req,
          label=req.member.display_name if req.member else '',
          new={'decision': decision, 'new': req.new_name},
          year=req.pujan_year)
    db.session.commit()
    # The answer is built before anybody is told, because telling them is the
    # part that can fail. A failed query leaves the session needing a
    # rollback, and reading the request off it afterwards would then raise -
    # turning "approved, but the SMS did not go" into a 500 on a change that
    # had in fact been applied.
    answer = {'success': True, 'request': req.to_dict()}

    told = []
    try:
        told = tell_member_decision(
            req, 'approved' if decision == 'approve' else 'rejected')
    except Exception as e:
        db.session.rollback()
        print(f'[requests] could not tell the member: {e.__class__.__name__}: {e}')
        answer['tell_error'] = f'{e.__class__.__name__}'
    answer['told'] = [{'channel': c, 'status': s} for c, s in told]
    return jsonify(answer)

@app.route('/api/event-config')
@login_required
def api_get_event_config():
    year = request.args.get('year', get_current_year(), type=int)
    return jsonify(get_event_config(year).to_dict())


@app.route('/api/event-config', methods=['PUT'])
@perm_required('event.config')
def api_update_event_config():
    data = request.get_json(silent=True) or {}
    year = int(data.get('pujan_year') or get_current_year())
    cfg = get_event_config(year)
    if 'event_date' in data:
        try:
            cfg.event_date = _parse_date(data['event_date']) if data['event_date'] else None
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
    for f in ('event_time', 'report_time', 'ho_venue', 'ho_address', 'center_venue',
              'contact_phone', 'samagri_gu', 'samagri_en', 'body_center_gu',
              'body_center_en', 'body_ho_gu', 'body_ho_en', 'subject_gu', 'subject_en'):
        if f in data:
            setattr(cfg, f, (data.get(f) or '').strip())
    for f in ('selfserve_open', 'selfserve_auto_add', 'selfserve_auto_edit',
              'selfserve_auto_email'):
        if f in data:
            setattr(cfg, f, bool(data.get(f)))
    if 'selfserve_until' in data:
        try:
            cfg.selfserve_until = (_parse_date(data['selfserve_until'])
                                   if data['selfserve_until'] else None)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
    if data.get('language') in ('gu', 'en'):
        cfg.language = data['language']
    db.session.commit()
    return jsonify({'success': True, 'config': cfg.to_dict()})


@app.route('/api/event-config/poster', methods=['POST'])
@perm_required('event.config')
def api_upload_poster():
    year = request.form.get('year', type=int) or get_current_year()
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'Choose an image first'}), 400
    data = f.read()
    if len(data) > 4 * 1024 * 1024:
        return jsonify({'error': 'The image must be under 4 MB'}), 400
    mime = (f.mimetype or '').lower()
    if not mime.startswith('image/'):
        return jsonify({'error': 'That file is not an image'}), 400
    cfg = get_event_config(year)
    old_path = cfg.poster_path or ''
    try:
        new_path = write_media_file(f'posters/{year}', f.filename, data)
    except OSError as e:
        app.logger.error('Poster media write failed: %s', e)
        return jsonify({'error': 'Persistent media storage is not writable'}), 503
    try:
        cfg.poster_path = new_path
        cfg.poster_data = None
        cfg.poster_mime, cfg.poster_name = mime, (f.filename or '')[:200]
        db.session.commit()
    except Exception:
        db.session.rollback()
        delete_media_file(new_path)
        raise
    if old_path and old_path != new_path:
        delete_media_file(old_path)
    return jsonify({'success': True, 'config': cfg.to_dict()})


@app.route('/api/event-config/poster', methods=['DELETE'])
@perm_required('event.config')
def api_delete_poster():
    cfg = get_event_config(request.args.get('year', get_current_year(), type=int))
    old_path = cfg.poster_path or ''
    cfg.poster_data = None
    cfg.poster_path = ''
    cfg.poster_mime = cfg.poster_name = None
    db.session.commit()
    delete_media_file(old_path)
    return jsonify({'success': True})


@app.route('/event-poster/<int:year>')
@login_required
def event_poster(year):
    cfg = EventConfig.query.filter_by(pujan_year=year).first()
    if not cfg:
        return jsonify({'error': 'No poster'}), 404
    blob = read_media_file(cfg.poster_path) if cfg.poster_path else cfg.poster_data
    if not blob:
        return jsonify({'error': 'No poster'}), 404
    return Response(blob, mimetype=cfg.poster_mime or 'image/jpeg')


def _msg_context(cfg, member, lang, audience, year):
    p = Pass.query.filter_by(member_id=member.id, pujan_year=year).first()
    return {
        'name': member.name,
        'member_code': member.member_code or '',
        'center': member.center.label if member.center else '',
        'year': year,
        'date': cfg.event_date.strftime('%d-%m-%Y') if cfg.event_date else '',
        'weekday': cfg.event_date.strftime('%A') if cfg.event_date else '',
        'time': cfg.event_time or '',
        'report_time': cfg.report_time or '',
        'venue': cfg.ho_venue if audience == 'ho' else (cfg.center_venue or ''),
        'ho_venue': cfg.ho_venue or '',
        'ho_address': cfg.ho_address or '',
        'contact': cfg.contact_phone or '',
        'samagri': (cfg.samagri_gu if lang == 'gu' else cfg.samagri_en) or '',
        'pass_number': p.pass_number if p else '-',
    }


def _template_for(cfg, lang, audience):
    key = f'body_{audience}_{lang}'
    return getattr(cfg, key, '') or DEFAULT_TEMPLATES.get(f'{audience}_{lang}', '')


def _audience_members(audience, year, cf):
    """'ho' = everyone ticked for Head Office. 'center' = everyone else."""
    q = Member.query.filter_by(is_active=True)
    if cf:
        q = q.filter_by(center_id=cf)
    if audience == 'ho':
        q = q.filter_by(attend_at_ho=True)
    elif audience == 'center':
        q = q.filter(db.or_(Member.attend_at_ho.is_(False), Member.attend_at_ho.is_(None)))
    return q.order_by(Member.center_id, Member.name).all()


# The fields a template can use. Kept in one place so the editor can list them
# and the sender can fill them.
COMM_PLACEHOLDERS = [
    ('name', "the member's name"),
    ('member_code', 'their Member ID'),
    ('center', 'their centre'),
    ('year', 'the pujan year'),
    ('years', 'every year this message covers'),
    ('pending_years', 'what is outstanding, year by year'),
    ('sankalp', 'their total pledge for the year'),
    ('collected', 'how much has come in'),
    ('pending', 'how much is still outstanding'),
    ('last_year', 'the year before the one being sent'),
    ('last_sankalp', 'what they pledged last year'),
    ('last_collected', 'what came in against it'),
    ('last_pending', 'what was still owed at the end of last year'),
    ('dharmada', 'dharmada amount'),
    ('dharmada_type', 'dharmada type (R / I/R / P / N)'),
    ('firms', 'their firms and companies, comma separated'),
    ('firm_count', 'how many firms and companies they have'),
    ('firm_list', 'their firms numbered: 1) Name 2) Name'),
    ('seat', 'seat number, once checked in'),
    ('pass_no', 'pass number, if one is issued'),
    ('qr', 'the QR image link, as words to click - "Click here for the QR '
           'code image"'),
    ('qr_url', 'the same address, bare, to lay out yourself'),
    ('my_link', 'their own page for correcting a firm name, as words to click'),
    ('my_link_url', 'the same address, bare'),
    ('date', 'the pujan date'),
    ('weekday', 'the day of the week'),
    ('time', 'the pujan time'),
    ('report_time', 'when to arrive'),
    ('venue', 'their centre by name, or the head centre if attending there'),
    ('venue_address', "the address of that venue"),
    ('ho_venue', 'the head centre venue, whoever is reading'),
    ('ho_address', 'the head centre address'),
    ('center_venue', "their own centre, blank for a head-centre member"),
    ('samagri', 'the samagri list'),
    ('contact', 'the number to ring: their centre accountant, or Head Office'),
    ('contact_name', 'the name that goes with it'),
    ('contact_person', 'name and number together'),
    ('center_contact', "their centre accountant's number, blank at Head Office"),
    ('center_contact_name', 'that accountant\'s name'),
]

DEFAULT_COMM_TEMPLATES = [
    dict(name='Sankalp confirmation (Gujarati)', name_gu='સંકલ્પ પુષ્ટિ (ગુજરાતી)', purpose='sankalp', channel='any',
         language='gu', subject='ચોપડા પૂજન {year} — સંકલ્પ',
         body='જય સ્વામિનારાયણ {name},\n\n'
              'ચોપડા પૂજન {year} માટે આપનો સંકલ્પ રૂ. {sankalp} નોંધાયો છે.\n'
              'પેઢી: {firms}\n\nસેન્ટર: {center}\nસભ્ય આઈડી: {member_code}\n\n'
              'આપનો સહકાર બદલ આભાર.\nSMVS'),
    dict(name='Sankalp confirmation (English)', name_gu='સંકલ્પ પુષ્ટિ (અંગ્રેજી)', purpose='sankalp', channel='any',
         language='en', subject='Chopda-Pujan {year} - your sankalp',
         body='Jay Swaminarayan {name},\n\n'
              'Your sankalp of Rs. {sankalp} for Chopda-Pujan {year} has been recorded.\n'
              'Firm: {firms}\n\nCenter: {center}\nMember ID: {member_code}\n\n'
              'Thank you.\nSMVS'),
    dict(name='Pending amount reminder (Gujarati)', name_gu='બાકી રકમ યાદી (ગુજરાતી)', purpose='pending', channel='any',
         language='gu', subject='ચોપડા પૂજન {year} — બાકી રકમ',
         body='જય સ્વામિનારાયણ {name},\n\n'
              'ચોપડા પૂજન {year} — સંકલ્પ રૂ. {sankalp}, જમા રૂ. {collected}, '
              'બાકી રૂ. {pending}.\n\n'
              'બાકી રકમ સેન્ટર પર જમા કરાવવા વિનંતી.\n{center}\nSMVS'),
    dict(name='Pending amount reminder (English)', name_gu='બાકી રકમ યાદી (અંગ્રેજી)', purpose='pending', channel='any',
         language='en', subject='Chopda-Pujan {year} - pending amount',
         body='Jay Swaminarayan {name},\n\n'
              'Chopda-Pujan {year} - sankalp Rs. {sankalp}, received Rs. {collected}, '
              'pending Rs. {pending}.\n\n'
              'Kindly deposit the balance at your center.\n{center}\nSMVS'),
    dict(name='Welcome on registration', name_gu='નોંધણી આવકાર',
         purpose='welcome', channel='whatsapp', language='en',
         subject='Welcome to Chopda-Pujan {year}',
         body='Jay Swaminarayan.. Welcome {name} to Chopdapujan {year}. '
              'Your registered center is {center}. '
              'You registered your following {firm_count} firm with us {firm_list}. '
              'Your *Pass No. is {pass_no}* and for which QR code is this.. {qr} '
              'Show this QR Code at the entrance gate for attendance. '
              'If you have any questions or suggestions please fill free to '
              'contact us via mk@smvs.org. Raji Rehjo.'),
    dict(name='Check-in confirmation (Gujarati)', name_gu='ચેક-ઇન પુષ્ટિ (ગુજરાતી)',
         purpose='checkin', channel='sms', language='gu',
         subject='ચોપડા પૂજન {year}',
         body='જય સ્વામિનારાયણ {name}, ચોપડા પૂજન {year} — આપની બેઠક નં. {seat}. '
              'પાસ {pass_no}. SMVS'),
    dict(name='Check-in confirmation (English)', name_gu='ચેક-ઇન પુષ્ટિ (અંગ્રેજી)',
         purpose='checkin', channel='sms', language='en',
         subject='Chopda-Pujan {year}',
         body='Jay Swaminarayan {name}, Chopda-Pujan {year} - your seat no. is '
              '{seat}. Pass {pass_no}. SMVS'),
    dict(name='Pujan details with poster (Gujarati)', name_gu='પૂજન વિગત પોસ્ટર સાથે (ગુજરાતી)', purpose='event', channel='any',
         language='gu', subject='ચોપડા પૂજન {year} — વિગત', include_poster=True,
         body='જય સ્વામિનારાયણ {name},\n\n'
              'ચોપડા પૂજન {date}, {weekday} — સમય {time}\n'
              'સ્થળ: {venue}\nપહોંચવાનો સમય: {report_time}\n\n'
              'સામગ્રી:\n{samagri}\n\nસંપર્ક: {contact}\nSMVS'),
    dict(name='Pujan details with poster (English)', name_gu='પૂજન વિગત પોસ્ટર સાથે (અંગ્રેજી)', purpose='event', channel='any',
         language='en', subject='Chopda-Pujan {year} - details', include_poster=True,
         body='Jay Swaminarayan {name},\n\n'
              'Chopda-Pujan on {date}, {weekday} at {time}\n'
              'Venue: {venue}\nPlease arrive by: {report_time}\n\n'
              'Samagri:\n{samagri}\n\nContact: {contact}\nSMVS'),
]


def seed_templates():
    """Puts the starting templates in place, once."""
    made = 0
    for spec in DEFAULT_COMM_TEMPLATES:
        if MessageTemplate.query.filter_by(name=spec['name']).first():
            continue
        db.session.add(MessageTemplate(is_system=True, **spec))
        made += 1
    if made:
        db.session.commit()
    return made


def event_poster(cfg, year):
    """The poster as (filename, mime, bytes), ready to attach.

    send_emails() unpacks its attachment into three, so handing it the raw
    bytes made it try to unpack the image itself - which is where "too many
    values to unpack (expected 3)" came from. Built in one place now, so the
    three senders cannot disagree about the shape again.
    """
    if not cfg:
        return None
    blob = read_media_file(cfg.poster_path) if cfg.poster_path else cfg.poster_data
    if not blob:
        return None
    return (cfg.poster_name or f'chopda-pujan-{year}.jpg',
            cfg.poster_mime or 'image/jpeg',
            blob)


def center_accountant(center):
    """The accountant to put in a message about a centre's own event.

    A member going to their own centre needs the name and number of somebody
    at that centre, not the head office line. Falls back to the centre's sant,
    then to the centre's own phone, so the message never goes out with nobody
    to ring.
    """
    if not center:
        return None, ''
    for role in ('center_accountant', 'center_sant'):
        u = User.query.filter_by(center_id=center.id, role=role,
                                 is_active=True).order_by(User.id).first()
        if u and (u.mobile or center.phone):
            return (u.full_name or u.username or ''), (u.mobile or center.phone or '')
    return '', (center.phone or '')


def event_venue(member, cfg, at_ho):
    """(venue, address, contact name, contact number) for one member.

    The head centre has one address for everybody sent there. Everybody else
    is told their own centre by name - a line reading "your own SMVS center"
    tells a member nothing they did not already know, and nothing they can
    give to a driver.
    """
    if at_ho:
        return ((cfg.ho_venue or '') if cfg else '',
                (cfg.ho_address or '') if cfg else '',
                '', (cfg.contact_phone or '') if cfg else '')
    c = member.center
    venue = (c.label if c else '') or ((cfg.center_venue or '') if cfg else '')
    if cfg and cfg.center_venue and not c:
        venue = cfg.center_venue
    name, phone = center_accountant(c)
    return (venue, (c.address or '') if c else '', name,
            phone or ((cfg.contact_phone or '') if cfg else ''))


# What the two links say instead of showing their address. An email gets a
# hyperlink; SMS and WhatsApp cannot, so they get the wording and then the
# address, which is the closest thing to it that survives plain text.
LINK_WORDING = {
    'my_link': {
        'en': 'Want to add or edit a firm name yourself? Click here',
        'gu': '\u0aaa\u0ac7\u0aa2\u0ac0\u0aa8\u0ac1\u0a82 \u0aa8\u0abe\u0aae '
              '\u0a9c\u0abe\u0aa4\u0ac7 \u0a89\u0aae\u0ac7\u0ab0\u0ab5\u0ac1\u0a82 '
              '\u0a95\u0ac7 \u0ab8\u0ac1\u0aa7\u0abe\u0ab0\u0ab5\u0ac1\u0a82 '
              '\u0a9b\u0ac7? \u0a85\u0ab9\u0ac0\u0a82 \u0a95\u0acd\u0ab2\u0abf\u0a95 '
              '\u0a95\u0ab0\u0acb',
    },
    'qr': {
        'en': 'Click here for the QR code image',
        'gu': 'QR \u0a95\u0acb\u0aa1 \u0a9c\u0acb\u0ab5\u0abe '
              '\u0a85\u0ab9\u0ac0\u0a82 \u0a95\u0acd\u0ab2\u0abf\u0a95 '
              '\u0a95\u0ab0\u0acb',
    },
}


def worded_link(key, url, lang, channel):
    """A link the reader can understand, for the channel it is going down."""
    if not url:
        return ''
    words = LINK_WORDING[key].get(lang) or LINK_WORDING[key]['en']
    if channel == 'email':
        return f'<a href="{url}">{words}</a>'
    return f'{words}: {url}'


def comm_context(member, year, cfg=None, years=None, channel=None):
    """Everything a template can substitute, for one member.

    `years` lets one message cover several pujan years at once - the figures
    are then the total across those years, and {pending_years} spells out the
    year-by-year breakdown, which is what a multi-year reminder needs.
    """
    cfg = cfg or get_event_config(year)
    span = sorted(set(years)) if years else [year]
    sks = Sankalp.query.filter(Sankalp.member_id == member.id,
                               Sankalp.pujan_year.in_(span)).all()
    total = sum(Decimal(str(x.amount)) for x in sks) if sks else Decimal('0')
    got = sum(sum(Decimal(str(c.amount)) for c in x.collections) for x in sks) if sks else Decimal('0')
    # The year-by-year breakdown, only for the years with something owing.
    lines = []
    for y in span:
        ys = [x for x in sks if x.pujan_year == y]
        if not ys:
            continue
        yt = sum(Decimal(str(x.amount)) for x in ys)
        yg = sum(sum(Decimal(str(c.amount)) for c in x.collections) for x in ys)
        if yt - yg > 0:
            lines.append(f'{y}: {float(yt - yg):,.0f}')
    # Last year on its own, whatever years this message covers. A reminder
    # that says "you gave this much last year" is a different sentence from
    # one about the years being sent, and mixing them into {sankalp} would
    # make both wrong.
    prev_year = min(span) - 1
    prev = Sankalp.query.filter_by(member_id=member.id,
                                   pujan_year=prev_year).all()
    prev_total = sum(Decimal(str(x.amount)) for x in prev) if prev else Decimal('0')
    prev_got = (sum(sum(Decimal(str(c.amount)) for c in x.collections)
                    for x in prev) if prev else Decimal('0'))
    att = Attendance.query.filter_by(member_id=member.id, pujan_year=year).first()
    pas = Pass.query.filter_by(member_id=member.id, pujan_year=year).first()
    at_ho = bool(member.attend_at_ho)
    venue, venue_addr, contact_name, contact_no = event_venue(member, cfg, at_ho)
    msg_lang = (cfg.language if cfg and cfg.language else DEFAULT_LANGUAGE)
    qr_url = (external_url('qr_image', token=qr_token(pas.id))
              if pas else '')
    my_url = (external_url('selfserve_page', token=selfserve_token(member.id))
              if selfserve_state(cfg)[0] else '')
    return {
        'name': member.name,
        'member_code': member.member_code or '',
        'center': member.center.label if member.center else '',
        'year': str(year),
        'years': ', '.join(str(y) for y in span),
        'pending_years': '\n'.join(lines),
        'sankalp': f'{float(total):,.0f}',
        'collected': f'{float(got):,.0f}',
        'pending': f'{float(total - got):,.0f}',
        'last_year': str(prev_year),
        'last_sankalp': f'{float(prev_total):,.0f}',
        'last_collected': f'{float(prev_got):,.0f}',
        'last_pending': f'{float(prev_total - prev_got):,.0f}',
        'dharmada': f'{float(member.dharmada_amount or 0):,.0f}',
        'dharmada_type': member.dharmada_type or '',
        'firms': ', '.join(e.name for e in member.entities) or member.name,
        'firm_count': str(len(member.entities)),
        'firm_list': ' '.join(f'{i}.) {e.name}'
                              for i, e in enumerate(member.entities, 1)) or member.name,
        'seat': att.seat_label if att else '',
        'pass_no': pas.pass_number if pas else '',
        # A signed link rather than the raw id, so the image is fetchable by a
        # mail client without a session and still not guessable.
        # {qr} reads as words; {qr_url} is the bare address, for a template
        # that would rather lay the link out itself.
        'qr': worded_link('qr', qr_url, msg_lang, channel),
        'qr_url': qr_url,
        # The member's own page, for correcting a firm name or adding one.
        # Blank once the window has closed, so a message sent afterwards does
        # not hand out a link that only says "closed".
        'my_link': worded_link('my_link', my_url, msg_lang, channel),
        'my_link_url': my_url,
        'date': cfg.event_date.strftime('%d-%m-%Y') if cfg and cfg.event_date else '',
        'weekday': cfg.event_date.strftime('%A') if cfg and cfg.event_date else '',
        'time': (cfg.event_time or '') if cfg else '',
        'report_time': (cfg.report_time or '') if cfg else '',
        'venue': venue,
        'venue_address': venue_addr,
        'ho_venue': (cfg.ho_venue or '') if cfg else '',
        'ho_address': (cfg.ho_address or '') if cfg else '',
        'center_venue': venue if not at_ho else '',
        'samagri': ((cfg.samagri_gu if cfg.language == 'gu' else cfg.samagri_en) or '') if cfg else '',
        # {contact} is whoever this member should ring: the centre accountant
        # for their own centre, the head office number for the head centre.
        'contact': contact_no,
        'contact_name': contact_name,
        # Name and number together, for a template that wants one placeholder.
        'contact_person': (f'{contact_name} - {contact_no}'.strip(' -')
                           if contact_name else contact_no),
        'center_contact': contact_no if not at_ho else '',
        'center_contact_name': contact_name if not at_ho else '',
    }


def selfserve_token(member_id):
    """A signed, expiring token for one member's own details page.

    Members have no login, and these links travel by WhatsApp where they get
    forwarded around. So the link carries a signature rather than an id: it
    cannot be guessed by counting upwards, it only ever opens the one member,
    and it stops working on its own.
    """
    return URLSafeTimedSerializer(app.config['SECRET_KEY'],
                                  salt='member-selfserve').dumps(int(member_id))


def selfserve_member_id(token):
    max_age = int(os.environ.get('SELFSERVE_LINK_DAYS', '180')) * 86400
    try:
        return URLSafeTimedSerializer(app.config['SECRET_KEY'],
                                      salt='member-selfserve').loads(
                                          token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def selfserve_state(cfg):
    """(open?, why not) for this year's self-service window."""
    if not cfg or not cfg.selfserve_open:
        return False, 'closed'
    if cfg.selfserve_until and date.today() > cfg.selfserve_until:
        return False, 'expired'
    return True, ''


def qr_token(pass_id):
    """A signed, expiring token for one pass's QR image.

    The image has to be fetchable without a session - it is loaded by a mail
    client or WhatsApp - so the URL is signed instead. Signing means a pass id
    cannot be guessed by counting upwards, and the link stops working after
    QR_LINK_DAYS.
    """
    return URLSafeTimedSerializer(app.config['SECRET_KEY'], salt='pass-qr').dumps(int(pass_id))


def qr_pass_id(token):
    max_age = int(os.environ.get('QR_LINK_DAYS', '120')) * 86400
    try:
        return URLSafeTimedSerializer(app.config['SECRET_KEY'],
                                      salt='pass-qr').loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def qr_png(pass_number, scale=7):
    """One pass's QR as PNG bytes.

    Shared by the web route and the mail sender. Attaching the image to the
    message rather than only linking to it is what makes it show up: a link
    is text, and a remote image is blocked by default in most mail clients
    even when the address is right.
    """
    buf = io.BytesIO()
    segno.make(pass_number, error='m').save(buf, kind='png', scale=scale,
                                            border=2, dark='#1a0a00',
                                            light='#ffffff')
    return buf.getvalue()


@app.route('/qr/<token>.png')
@csrf.exempt
def qr_image(token):
    """The QR for one pass, as a PNG. Deliberately open to anyone holding the
    signed link, because an email client fetches it with no cookies."""
    pid = qr_pass_id(token)
    if pid is None:
        abort(404)
    p = db.session.get(Pass, pid)
    if not p:
        abort(404)
    resp = send_file(io.BytesIO(qr_png(p.pass_number, scale=8)),
                     mimetype='image/png',
                     download_name=f'{p.pass_number}.png')
    # Long cache: the code for a given pass never changes.
    resp.headers['Cache-Control'] = 'public, max-age=604800'
    return resp


DEFAULT_INSTRUCTIONS = [
    dict(title='Before you start', sort_order=10, audience='all', is_system=True,
         title_gu='શરૂ કરતાં પહેલાં',
         body="""<h3>Welcome to the SMVS Chopda-Pujan system</h3>
<p>A few things worth knowing before you begin.</p>
<ul>
  <li><b>What you can see is what you are allowed to see.</b> Your menu, your
      buttons and even the amount columns follow the rights your role has been
      given. If something is missing, it was not given to you - ask, rather
      than working around it.</li>
  <li><b>Every change is recorded.</b> Who changed an amount, when, and what it
      was before, all sit in the Activity Log. This protects you as much as
      anyone: if a figure is questioned, the record shows what happened.</li>
  <li><b>Do not share your login.</b> Two people on one account means the log
      cannot tell you apart. Ask for your own.</li>
  <li><b>Change your password</b> the first time you sign in.</li>
</ul>
<p>If something looks wrong, say so early. A figure corrected on the day is a
small thing; the same figure found in March is not.</p>""",
         body_gu="""<h3>SMVS ચોપડા-પૂજન સિસ્ટમમાં આપનું સ્વાગત છે</h3>
<p>શરૂ કરતાં પહેલાં થોડી વાત.</p>
<ul>
  <li><b>જે દેખાય છે તે જ આપને જોવાની પરવાનગી છે.</b> મેનુ, બટન અને રકમના
      કૉલમ આપની ભૂમિકાના અધિકાર પ્રમાણે દેખાય છે. કંઈ ન દેખાય તો પૂછો.</li>
  <li><b>દરેક ફેરફાર નોંધાય છે.</b> કોણે, ક્યારે, પહેલાં શું હતું - બધું
      Activity Log માં રહે છે.</li>
  <li><b>લોગિન કોઈને આપશો નહીં.</b> એક ખાતામાં બે જણ હોય તો નોંધ કોની છે તે
      ખબર ન પડે.</li>
  <li>પહેલી વાર લોગિન કરો ત્યારે <b>પાસવર્ડ બદલો</b>.</li>
</ul>"""),

    dict(title='Working at your center', sort_order=20, audience='center',
         is_system=True, title_gu='આપના સેન્ટર પર કામ',
         body="""<h3>What your login covers</h3>
<p>You are signed in for <b>one center</b>. Members, pledges, collections,
passes and attendance are all limited to it - you will not see another center's
figures, and you cannot enter anything against them.</p>
<h4>The usual order of work</h4>
<ol>
  <li><b>Members</b> - add the people of your center. Fast Entry is quicker for
      a long list; the full form is for someone with firms and partners.</li>
  <li><b>Sankalp</b> - record what each person has pledged. Bulk edit is the
      fastest way through a stack of slips.</li>
  <li><b>Collection</b> - enter money as it comes in. Individual clubs a
      member's firms into one figure; Firm wise keeps them separate.</li>
  <li><b>Passes</b> - generate them once the pledges are in, then print.</li>
  <li><b>Attendance</b> - on the day, scan or type the pass number. A seat
      number is issued automatically.</li>
</ol>
<h4>On the day</h4>
<p>If somebody arrives who is not on the list, use <b>Fast Entry with "Today is
the event date"</b> ticked. They get a pass, a seat and a check-in in one step,
and appear on the Sankalp list at zero so you can enter their pledge
afterwards.</p>""",
         body_gu="""<h3>આપના લોગિનનો વ્યાપ</h3>
<p>આપ <b>એક સેન્ટર</b> માટે લોગિન થયા છો. સભ્ય, સંકલ્પ, જમા, પાસ અને હાજરી -
બધું તે સેન્ટર પૂરતું જ. બીજા સેન્ટરની વિગત દેખાશે નહીં.</p>
<h4>કામનો ક્રમ</h4>
<ol>
  <li><b>સભ્ય</b> ઉમેરો - લાંબી યાદી માટે Fast Entry, પેઢી-ભાગીદાર માટે
      આખું ફોર્મ.</li>
  <li><b>સંકલ્પ</b> નોંધો - ઢગલો સ્લિપ માટે Bulk edit સૌથી ઝડપી.</li>
  <li><b>જમા</b> - રકમ આવે તેમ નોંધો.</li>
  <li><b>પાસ</b> બનાવો અને પ્રિન્ટ કરો.</li>
  <li><b>હાજરી</b> - પૂજનના દિવસે QR સ્કેન કરો કે પાસ નંબર ટાઈપ કરો. બેઠક
      નંબર જાતે મળી જશે.</li>
</ol>
<h4>પૂજનના દિવસે</h4>
<p>યાદીમાં ન હોય તેવી વ્યક્તિ આવે તો <b>Fast Entry</b> માં "Today is the event
date" ટીક કરો. પાસ, બેઠક અને હાજરી એક જ સાથે થઈ જશે.</p>"""),

    dict(title='Working across centers', sort_order=20, audience='team_member',
         is_system=True, title_gu='બધા સેન્ટર માટે કામ',
         body="""<h3>What your login covers</h3>
<p>You are signed in as a <b>Team Member</b>, so you see <b>every center</b>.
That is a good deal of reach: an amount you change belongs to somebody else's
center, and their totals move with it.</p>
<h4>Worth being careful with</h4>
<ul>
  <li><b>Head Office attendance.</b> Ticking a member "at HO" moves them out of
      their center's list and into yours. It changes both sets of numbers.</li>
  <li><b>Registering on the day.</b> With "Today is the event date" ticked,
      anyone you register counts at <b>Head Office</b>, because that is where
      you are standing. A center user registering the same person would put
      them at their own center.</li>
  <li><b>Pass numbering.</b> Set it before generating any passes. Changing it
      afterwards makes the sequence jump.</li>
  <li><b>Bulk messages.</b> Preview before sending. The count on the preview is
      how many people will actually receive it.</li>
</ul>
<h4>The reports are the point</h4>
<p>Center Summary and Attendance are what Head Office runs the day on. Every
number on the attendance grid can be clicked to see exactly who it refers
to.</p>""",
         body_gu="""<h3>આપના લોગિનનો વ્યાપ</h3>
<p>આપ <b>Team Member</b> તરીકે લોગિન થયા છો, એટલે <b>બધા સેન્ટર</b> દેખાશે.
આપ જે રકમ બદલો તે કોઈ સેન્ટરની છે અને તેમના આંકડા પણ બદલાશે.</p>
<h4>ધ્યાન રાખવા જેવું</h4>
<ul>
  <li><b>હેડ ઓફિસ હાજરી</b> - સભ્યને "at HO" ટીક કરવાથી તે તેમના સેન્ટરની
      યાદીમાંથી નીકળી આપની યાદીમાં આવે છે.</li>
  <li><b>પૂજનના દિવસે નોંધણી</b> - "Today is the event date" સાથે આપ જેને
      નોંધો તે <b>હેડ ઓફિસ</b> માં ગણાશે.</li>
  <li><b>પાસ નંબરિંગ</b> પાસ બનાવતાં પહેલાં ગોઠવો.</li>
  <li><b>એકસાથે સંદેશા</b> - મોકલતાં પહેલાં Preview જુઓ.</li>
</ul>"""),

    dict(title='Handling money', sort_order=30, audience='all', is_system=True,
         any_perms=json.dumps(['finance.sankalp', 'collection.create']),
         title_gu='રકમ સંભાળવી',
         body="""<h3>Pledges and collections</h3>
<p>You can see and enter amounts, so a few rules.</p>
<ul>
  <li><b>Enter what the slip says</b>, not what you remember. If the slip is
      unclear, leave it and ask.</li>
  <li><b>Revising a pledge needs a reason.</b> The system will ask for a remark
      when you change an amount that is already recorded. That remark is the
      only explanation anyone will have later - "correction" tells them
      nothing.</li>
  <li><b>A collection cannot exceed what is pending.</b> If it will not accept
      the figure, the pledge is probably wrong, not the payment.</li>
  <li><b>Never delete to fix a mistake</b> if you can edit instead. An edit
      keeps the history; a delete loses it.</li>
</ul>
<p>Cash you have accepted is your responsibility until it is entered. Enter it
the same day.</p>""",
         body_gu="""<h3>સંકલ્પ અને જમા</h3>
<ul>
  <li><b>સ્લિપમાં લખ્યું હોય તે જ ભરો</b>, યાદદાસ્તથી નહીં.</li>
  <li><b>સંકલ્પ બદલવા કારણ જોઈએ.</b> નોંધાયેલી રકમ બદલશો ત્યારે નોંધ માંગશે -
      તે જ પછીથી એકમાત્ર ખુલાસો હશે.</li>
  <li><b>બાકી રકમથી વધારે જમા ન થાય.</b> ન સ્વીકારે તો સંકલ્પ ખોટો હશે.</li>
  <li>ભૂલ સુધારવા <b>ડિલીટ ન કરો</b>, એડિટ કરો.</li>
</ul>
<p>સ્વીકારેલી રકમ તે જ દિવસે નોંધો.</p>"""),

    dict(title='At the gate', sort_order=40, audience='all', is_system=True,
         any_perms=json.dumps(['attendance.checkin']),
         title_gu='દરવાજા પર',
         body="""<h3>Checking people in</h3>
<ul>
  <li><b>Scan the QR, or type the number.</b> Just the digits will do - 501
      finds YJ0501.</li>
  <li><b>Read the seat number out.</b> It appears in large type the moment the
      check-in succeeds. That number is the point of the whole exercise.</li>
  <li><b>One message only.</b> If it refuses, the panel tells you why - already
      checked in, wrong year, no such pass. Read it rather than trying
      again.</li>
  <li><b>Full-screen mode</b> hides everything but the scanner and the keypad.
      Use it; it is faster and there is less to hit by accident.</li>
</ul>
<p>Nobody is turned away for a system problem. If the app will not cooperate,
write the name and pass number on paper and enter it later.</p>""",
         body_gu="""<h3>ચેક-ઇન</h3>
<ul>
  <li><b>QR સ્કેન કરો કે નંબર ટાઈપ કરો.</b> ફક્ત આંકડા ચાલશે - 501 થી YJ0501
      મળી જશે.</li>
  <li><b>બેઠક નંબર બોલીને કહો.</b> ચેક-ઇન થતાં જ મોટા અક્ષરે દેખાશે.</li>
  <li><b>એક જ સંદેશો દેખાશે.</b> ન સ્વીકારે તો કારણ લખેલું હશે - વાંચો.</li>
  <li><b>ફુલ-સ્ક્રીન મોડ</b> વાપરો - ઝડપી અને ભૂલ ઓછી.</li>
</ul>
<p>સિસ્ટમની તકલીફ માટે કોઈને પાછા ન કાઢો. કાગળ પર નામ-નંબર લખી પછી ભરો.</p>"""),
]


def seed_instructions():
    made = 0
    for spec in DEFAULT_INSTRUCTIONS:
        if Instruction.query.filter_by(title=spec['title']).first():
            continue
        db.session.add(Instruction(**spec))
        made += 1
    if made:
        db.session.commit()
    return made


def normalize_user_language(force=False):
    """Brings stored interface languages back in line with DEFAULT_LANGUAGE.

    Accounts seeded while the default was Gujarati keep language='gu' in the
    database for ever, because login trusts the stored value - which is why
    changing DEFAULT_LANGUAGE alone appears to do nothing on an existing
    installation. Rows with a missing or unrecognised code are always
    repaired. RESET_USER_LANGUAGE=1 additionally resets accounts that hold a
    valid code, for the one deploy where you want everybody moved over; leave
    it off afterwards so individual choices are respected again.
    """
    fixed = 0
    for u in User.query.all():
        bad = u.language not in ('en', 'gu')
        if bad or (force and u.language != DEFAULT_LANGUAGE):
            u.language = DEFAULT_LANGUAGE
            fixed += 1
    if fixed:
        db.session.commit()
        print('[init] Interface language set to %r for %d account(s)'
              % (DEFAULT_LANGUAGE, fixed))
    return fixed


def instructions_for(user, year=None, only_unaccepted=True):
    """The pages this user should see, in order."""
    year = year or get_current_year()
    rows = Instruction.query.filter_by(is_active=True)\
        .order_by(Instruction.sort_order, Instruction.id).all()
    mine = [i for i in rows if i.applies_to(user, year)]
    if not only_unaccepted:
        return mine
    accepted = {(a.instruction_id, a.version) for a in
                InstructionAcceptance.query.filter_by(user_id=user.id).all()}
    return [i for i in mine
            if i.require_accept and (i.id, i.version) not in accepted]


# Filenames the seeder recognises, so dropping a PDF in manuals/ is enough.
MANUAL_SEEDS = [
    ('manual_center_users.pdf', dict(
        title='User Manual — Center users', title_gu='યુઝર મેન્યુઅલ — સેન્ટર યુઝર',
        about='Members, Sankalp, Collection, passes and the gate, for one center.',
        about_gu='સભ્ય, સંકલ્પ, જમા, પાસ અને દરવાજો - એક સેન્ટર માટે.',
        audience='center', sort_order=10)),
    ('manual_team_members.pdf', dict(
        title='User Manual — Team Members', title_gu='યુઝર મેન્યુઅલ — ટીમ મેમ્બર',
        about='Working across every center: Head Office attendance, pass '
              'numbering, reports, messages and schedules.',
        about_gu='બધા સેન્ટર માટે: હેડ ઓફિસ હાજરી, પાસ નંબરિંગ, રિપોર્ટ, સંદેશા.',
        audience='team_member', sort_order=10)),
    ('manual_full.pdf', dict(
        title='User Manual — complete', title_gu='યુઝર મેન્યુઅલ — સંપૂર્ણ',
        about='Both tracks in one document, for reference.',
        about_gu='બંને ભાગ એક જ દસ્તાવેજમાં, સંદર્ભ માટે.',
        audience='all', sort_order=90)),
]


def seed_manual_speech(doc, path):
    """Loads the plain-text sidecars, if they were shipped with the PDFs.

    manual_full.pdf next to manual_full.txt, and manual_full_gu.pdf next to
    manual_full_gu.txt. One page of the manual per line.
    """
    got = False
    for attr, pdf_path in (('speech_en', path),
                           ('speech_gu', re.sub(r'\.pdf$', '_gu.pdf', path,
                                                flags=re.I))):
        txt_path = re.sub(r'\.pdf$', '.txt', pdf_path, flags=re.I)
        if not os.path.exists(txt_path):
            continue
        with io.open(txt_path, encoding='utf-8') as fh:
            body = fh.read().strip()
        if body:
            setattr(doc, attr, body)
            got = True
    return got


def seed_manual_gu(doc, path):
    """Loads the Gujarati twin of a manual file, if it is sitting beside it.

    manual_full.pdf and manual_full_gu.pdf, in the same folder. Keeps the
    two-file arrangement out of the seed table - the naming says which is
    which.
    """
    gu_path = re.sub(r'\.pdf$', '_gu.pdf', path, flags=re.I)
    if not os.path.exists(gu_path):
        return False
    with open(gu_path, 'rb') as fh:
        blob = fh.read()
    if not blob.startswith(b'%PDF-'):
        return False
    doc.data_gu = blob
    doc.size_gu = len(blob)
    doc.filename_gu = os.path.basename(gu_path)
    doc.version_gu = datetime.now(IST).strftime('%d-%m-%Y %H:%M')
    return True


def seed_manuals():
    """Loads any manual PDFs shipped with the build, once.

    Only fills a row that has no file yet, so an uploaded replacement is never
    overwritten by a redeploy.
    """
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'manuals')
    made = 0
    for fname, spec in MANUAL_SEEDS:
        path = os.path.join(folder, fname)
        row = ManualDoc.query.filter_by(title=spec['title']).first()
        # A row with an English file already loaded is left alone, except that
        # a Gujarati twin shipped later is still picked up - otherwise adding
        # the Gujarati edition would mean deleting the manual first.
        if row and (row.file_path or row.data):
            if os.path.exists(path):
                changed = False
                if not (row.file_path_gu or row.data_gu) and seed_manual_gu(row, path):
                    changed = True
                    print('[init] Loaded the Gujarati edition of %r'
                          % spec['title'])
                if not (row.speech_gu or '').strip() \
                        and seed_manual_speech(row, path):
                    changed = True
                if changed:
                    db.session.commit()
            continue
        if not os.path.exists(path):
            continue
        with open(path, 'rb') as fh:
            blob = fh.read()
        if row is None:
            row = ManualDoc(**spec)
            db.session.add(row)
        row.filename = fname
        row.mime = 'application/pdf'
        row.data = blob
        row.size = len(blob)
        row.version = datetime.now(IST).strftime('%d-%m-%Y')
        seed_manual_gu(row, path)
        seed_manual_speech(row, path)
        made += 1
    if made:
        db.session.commit()
    return made


def manuals_for(user):
    rows = ManualDoc.query.filter_by(is_active=True)\
        .order_by(ManualDoc.sort_order, ManualDoc.id).all()
    return [m for m in rows if m.applies_to(user)]


@app.route('/api/manuals')
@perm_required('manual.view', 'manual.manage')
def api_manuals():
    """The manuals this user may open."""
    user = User.query.get(session['user_id'])
    lang = session.get('lang') or user.language or DEFAULT_LANGUAGE
    return jsonify({'rows': [m.to_dict(lang) for m in manuals_for(user)]})


def manual_blob(doc, lang):
    """The bytes to serve, and what to call them.

    Gujarati when it was asked for and a Gujarati file exists; otherwise
    English, because a manual that opens in the wrong language is still better
    than a broken link.
    """
    if lang == 'gu' and (doc.file_path_gu or doc.data_gu):
        blob = read_media_file(doc.file_path_gu) if doc.file_path_gu else doc.data_gu
        if blob:
            return blob, (doc.filename_gu or 'manual-gu.pdf')
    blob = read_media_file(doc.file_path) if doc.file_path else doc.data
    return blob, (doc.filename or 'manual.pdf')


@app.route('/manual/<int:mid>')
@perm_required('manual.view', 'manual.manage')
def manual_file(mid):
    """The PDF itself, inline so the browser's own viewer opens it.

    Checked against this user's targeting rather than served on the id alone -
    the id is guessable, and a Team Member manual is not for a centre
    volunteer.
    """
    doc = ManualDoc.query.get_or_404(mid)
    user = User.query.get(session['user_id'])
    if not (doc.applies_to(user) or has_perm(user, 'manual.manage')):
        return jsonify({'error': 'That manual is not for your role'}), 403
    blob, name = manual_blob(doc, request.args.get('lang')
                             or session.get('lang') or user.language)
    if not blob:
        abort(404)
    resp = send_file(io.BytesIO(blob), mimetype=doc.mime or 'application/pdf',
                     download_name=name,
                     as_attachment=request.args.get('download') == '1')
    resp.headers['Cache-Control'] = 'private, max-age=600'
    return resp


@app.route('/manual/<int:mid>/text')
@perm_required('manual.view', 'manual.manage')
def manual_text(mid):
    """The manual as plain text, a page at a time, for reading aloud.

    The browser can speak text but cannot get it out of a PDF, so the text is
    lifted here. A page at a time rather than the whole document: a volunteer
    listens to the part they are on, and shipping fifty pages of text to speak
    three of them would be waste.

    Reading aloud is what makes this usable by somebody who cannot easily read
    a screen, which is a good part of who these manuals are for.
    """
    doc = ManualDoc.query.get_or_404(mid)
    user = User.query.get(session['user_id'])
    if not (doc.applies_to(user) or has_perm(user, 'manual.manage')):
        return jsonify({'error': 'That manual is not for your role'}), 403
    lang = request.args.get('lang') or session.get('lang') or user.language
    served_gu = bool(lang == 'gu' and (doc.file_path_gu or doc.data_gu))
    stored = (doc.speech_gu if served_gu else doc.speech_en) or ''
    if stored.strip():
        pages = stored.split('\n')
        total = len(pages)
        page = max(1, min(request.args.get('page', 1, type=int), total))
        text = pages[page - 1]
    else:
        # No sidecar shipped with this manual, so fall back to lifting the
        # text out of the PDF. Good enough for English; a Gujarati PDF read
        # this way will come out garbled, which is exactly why the sidecar
        # exists.
        blob, _ = manual_blob(doc, lang)
        if not blob:
            return jsonify({'error': 'No file to read'}), 404
        try:
            from pypdf import PdfReader
        except ImportError:
            return jsonify({'error': 'Reading aloud is not available on this '
                                     'server'}), 501
        try:
            reader = PdfReader(io.BytesIO(blob))
            total = len(reader.pages)
            page = max(1, min(request.args.get('page', 1, type=int), total))
            text = reader.pages[page - 1].extract_text() or ''
        except Exception:
            return jsonify({'error': 'That PDF could not be read as text'}), 422
    # Collapsed to single spaces: a PDF's line breaks are where the line ended
    # on the page, not where a sentence did, and read aloud they come out as
    # unnatural pauses.
    text = re.sub(r'[ \t]*\n[ \t]*', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text).strip()
    return jsonify({'page': page, 'pages': total, 'lang': lang,
                    'served_gu': served_gu, 'text': text})


@app.route('/api/manuals', methods=['POST'])
@perm_required('manual.manage')
def api_upload_manual():
    """Upload or replace a manual. Multipart, because it carries the file."""
    f = request.files.get('file')
    f_gu = request.files.get('file_gu')
    d = request.form
    title = (d.get('title') or '').strip()
    if not title:
        return jsonify({'error': 'Give the manual a title'}), 400
    mid = d.get('id')
    doc = ManualDoc.query.get(int(mid)) if mid else None
    if doc is None:
        doc = ManualDoc(title=title)
        db.session.add(doc)
    doc.title = title
    doc.title_gu = (d.get('title_gu') or '').strip()
    doc.about = (d.get('about') or '').strip()
    doc.about_gu = (d.get('about_gu') or '').strip()
    doc.audience = d.get('audience') or 'all'
    doc.role_keys = json.dumps(json.loads(d.get('roles') or '[]'))
    doc.any_perms = json.dumps(json.loads(d.get('any_perms') or '[]'))
    doc.sort_order = int(d.get('sort_order') or 100)
    doc.is_active = (d.get('is_active') or '1') == '1'

    def read_pdf(upload, label):
        """Reads and checks one uploaded PDF. Returns (blob, error)."""
        blob = upload.read()
        if not blob:
            return None, f'The {label} file is empty'
        # A PDF starts with %PDF-. Checked because a mislabelled file would
        # only fail later, in the viewer, where the reason is not obvious.
        if not blob.startswith(b'%PDF-'):
            return None, f'The {label} file is not a PDF'
        cap = app.config.get('MAX_CONTENT_LENGTH') or (12 * 1024 * 1024)
        if len(blob) > cap:
            return None, (f'The {label} file is too large. The limit is '
                          f'{cap // 1024 // 1024} MB')
        return blob, None

    new_paths = []
    old_paths = []
    if f and f.filename:
        blob, err = read_pdf(f, 'English')
        if err:
            return jsonify({'error': err}), 400
        try:
            new_path = write_media_file('manuals', f.filename or 'manual.pdf', blob)
        except OSError as e:
            app.logger.error('Manual media write failed: %s', e)
            return jsonify({'error': 'Persistent media storage is not writable'}), 503
        new_paths.append(new_path)
        if doc.file_path:
            old_paths.append(doc.file_path)
        doc.file_path = new_path
        doc.data = None
        doc.size = len(blob)
        doc.filename = (f.filename or 'manual.pdf')[:200]
        doc.mime = 'application/pdf'
        doc.version = datetime.now(IST).strftime('%d-%m-%Y %H:%M')
        doc.uploaded_by = session['user_id']
    elif not (doc.file_path or doc.data):
        return jsonify({'error': 'Choose a PDF to upload'}), 400

    if f_gu and f_gu.filename:
        blob, err = read_pdf(f_gu, 'Gujarati')
        if err:
            for path in new_paths:
                delete_media_file(path)
            return jsonify({'error': err}), 400
        try:
            new_path_gu = write_media_file('manuals', f_gu.filename or 'manual-gu.pdf', blob)
        except OSError as e:
            for path in new_paths:
                delete_media_file(path)
            app.logger.error('Manual media write failed: %s', e)
            return jsonify({'error': 'Persistent media storage is not writable'}), 503
        new_paths.append(new_path_gu)
        if doc.file_path_gu:
            old_paths.append(doc.file_path_gu)
        doc.file_path_gu = new_path_gu
        doc.data_gu = None
        doc.size_gu = len(blob)
        doc.filename_gu = (f_gu.filename or 'manual-gu.pdf')[:200]
        doc.version_gu = datetime.now(IST).strftime('%d-%m-%Y %H:%M')
        doc.uploaded_by = session['user_id']

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        for path in new_paths:
            delete_media_file(path)
        raise
    for path in old_paths:
        if path not in new_paths:
            delete_media_file(path)
    audit('update' if mid else 'create', 'manual', doc, label=doc.title)
    db.session.commit()
    return jsonify({'success': True, 'manual': doc.to_dict('en')})


@app.route('/api/manuals/all')
@perm_required('manual.manage')
def api_manuals_all():
    rows = ManualDoc.query.order_by(ManualDoc.sort_order, ManualDoc.id).all()
    return jsonify({'rows': [m.to_dict('en') for m in rows],
                    'roles': [{'key': r.key, 'name': r.name}
                              for r in Role.query.order_by(Role.sort_order).all()]})


@app.route('/api/manuals/<int:mid>', methods=['DELETE'])
@perm_required('manual.manage')
def api_delete_manual(mid):
    doc = ManualDoc.query.get_or_404(mid)
    paths = [p for p in (doc.file_path, doc.file_path_gu) if p]
    audit('delete', 'manual', doc, old={'title': doc.title}, label=doc.title)
    db.session.delete(doc)
    db.session.commit()
    for path in paths:
        delete_media_file(path)
    return jsonify({'success': True})


@app.route('/api/instructions/pending')
@login_required
def api_instructions_pending():
    """What this user has to read before the app opens."""
    user = User.query.get(session['user_id'])
    lang = session.get('lang') or user.language or DEFAULT_LANGUAGE
    pending = instructions_for(user)
    return jsonify({'rows': [i.to_dict(lang) for i in pending],
                    'count': len(pending)})


@app.route('/api/instructions/mine')
@login_required
def api_instructions_mine():
    """Everything that applies to this user, accepted or not, so it can be
    reread from the menu at any time."""
    user = User.query.get(session['user_id'])
    lang = session.get('lang') or user.language or DEFAULT_LANGUAGE
    mine = instructions_for(user, only_unaccepted=False)
    accepted = {(a.instruction_id, a.version): a for a in
                InstructionAcceptance.query.filter_by(user_id=user.id).all()}
    out = []
    for i in mine:
        d = i.to_dict(lang)
        a = accepted.get((i.id, i.version))
        d['accepted_at'] = to_ist(a.accepted_at, '%d-%m-%Y %H:%M') if a else None
        out.append(d)
    return jsonify({'rows': out})


@app.route('/api/instructions/accept', methods=['POST'])
@login_required
def api_instructions_accept():
    """Records acceptance. Only the pages actually shown to this user count."""
    ids = (request.get_json(silent=True) or {}).get('ids') or []
    user = User.query.get(session['user_id'])
    allowed = {i.id: i for i in instructions_for(user, only_unaccepted=False)}
    saved = 0
    for iid in ids:
        ins = allowed.get(int(iid))
        if not ins:
            continue
        if InstructionAcceptance.query.filter_by(
                user_id=user.id, instruction_id=ins.id, version=ins.version).first():
            continue
        db.session.add(InstructionAcceptance(
            user_id=user.id, instruction_id=ins.id, version=ins.version,
            ip=request.headers.get('X-Forwarded-For', request.remote_addr or '')[:60]))
        saved += 1
    if saved:
        db.session.commit()
        audit('create', 'instruction_accept', None, label=f'{saved} page(s)',
              new={'ids': ids})
        db.session.commit()
    remaining = len(instructions_for(user))
    return jsonify({'success': True, 'accepted': saved, 'remaining': remaining})


@app.route('/api/instructions')
@perm_required('instructions.edit')
def api_instructions():
    rows = Instruction.query.order_by(Instruction.sort_order, Instruction.id).all()
    return jsonify({'rows': [i.to_dict('en') for i in rows],
                    'roles': [{'key': r.key, 'name': r.name}
                              for r in Role.query.order_by(Role.sort_order).all()],
                    # Grouped the same way the rights screen shows them, so
                    # picking one to target is not a hunt through 51 keys.
                    'permissions': [{'group': gname,
                                     'items': [{'key': k, 'label': lbl} for k, lbl in items]}
                                    for _, gname, items in PERMISSION_GROUPS]})


@app.route('/api/instructions', methods=['POST'])
@perm_required('instructions.edit')
def api_create_instruction():
    d = request.get_json(silent=True) or {}
    if not (d.get('title') or '').strip() or not (d.get('body') or '').strip():
        return jsonify({'error': 'An instruction needs a title and a body'}), 400
    ins = Instruction(
        title=d['title'].strip(), title_gu=(d.get('title_gu') or '').strip(),
        body=d['body'], body_gu=d.get('body_gu') or '',
        audience=(d.get('audience') or 'all'),
        role_keys=json.dumps(d.get('roles') or []),
        any_perms=json.dumps(d.get('any_perms') or []),
        all_perms=json.dumps(d.get('all_perms') or []),
        require_accept=bool(d.get('require_accept', True)),
        sort_order=int(d.get('sort_order') or 100),
        pujan_year=d.get('pujan_year') or None)
    db.session.add(ins)
    db.session.commit()
    audit('create', 'instruction', ins, label=ins.title)
    db.session.commit()
    return jsonify({'success': True, 'instruction': ins.to_dict('en')})


@app.route('/api/instructions/<int:iid>', methods=['PUT'])
@perm_required('instructions.edit')
def api_update_instruction(iid):
    d = request.get_json(silent=True) or {}
    ins = Instruction.query.get_or_404(iid)
    before = {'title': ins.title, 'version': ins.version}
    for f in ('title', 'title_gu', 'body', 'body_gu', 'audience'):
        if f in d and d[f] is not None:
            setattr(ins, f, d[f].strip() if f.startswith('title') or f == 'audience' else d[f])
    for f, col in (('roles', 'role_keys'), ('any_perms', 'any_perms'),
                   ('all_perms', 'all_perms')):
        if f in d:
            setattr(ins, col, json.dumps(d.get(f) or []))
    if 'require_accept' in d:
        ins.require_accept = bool(d['require_accept'])
    if 'is_active' in d:
        ins.is_active = bool(d['is_active'])
    if 'sort_order' in d:
        ins.sort_order = int(d['sort_order'] or 100)
    if 'pujan_year' in d:
        ins.pujan_year = d['pujan_year'] or None
    # Raising the version is how everyone is asked to accept again.
    if d.get('bump_version'):
        ins.version = (ins.version or 1) + 1
    if not (ins.title or '').strip() or not (ins.body or '').strip():
        return jsonify({'error': 'An instruction needs a title and a body'}), 400
    db.session.commit()
    audit('update', 'instruction', ins, old=before, label=ins.title,
          new={'title': ins.title, 'version': ins.version})
    db.session.commit()
    return jsonify({'success': True, 'instruction': ins.to_dict('en')})


@app.route('/api/instructions/<int:iid>', methods=['DELETE'])
@perm_required('instructions.edit')
def api_delete_instruction(iid):
    ins = Instruction.query.get_or_404(iid)
    if ins.is_system:
        return jsonify({'error': 'A built-in page cannot be deleted. '
                                 'Edit it, or switch it off.'}), 400
    InstructionAcceptance.query.filter_by(instruction_id=ins.id).delete()
    audit('delete', 'instruction', ins, old={'title': ins.title}, label=ins.title)
    db.session.delete(ins)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/instructions/<int:iid>/who')
@perm_required('instructions.edit', 'instructions.report')
def api_instruction_who(iid):
    """Which users this page reaches, and which of them have accepted it.

    Worth being able to check: the audience is worked out from rights, so it is
    not obvious from the settings alone who will actually see it.
    """
    ins = Instruction.query.get_or_404(iid)
    accepted = {a.user_id: a for a in InstructionAcceptance.query.filter_by(
        instruction_id=ins.id, version=ins.version).all()}
    rows = []
    for u in User.query.filter_by(is_active=True).order_by(User.full_name).all():
        if not ins.applies_to(u):
            continue
        a = accepted.get(u.id)
        rows.append({'user_id': u.id, 'username': u.username,
                     'full_name': u.full_name,
                     'user_type': 'center' if is_center_scoped(u) else 'team_member',
                     'center_name': u.center.label if u.center else '',
                     'role': u.role,
                     'accepted_at': to_ist(a.accepted_at, '%d-%m-%Y %H:%M') if a else None})
    return jsonify({'instruction': ins.to_dict('en'), 'rows': rows,
                    'total': len(rows),
                    'accepted': sum(1 for r in rows if r['accepted_at'])})


@app.route('/api/templates')
@perm_required('comm.templates_view', 'comm.templates')
def api_templates():
    year = request.args.get('year', type=int)
    q = MessageTemplate.query
    purpose = request.args.get('purpose')
    if purpose:
        q = q.filter_by(purpose=purpose)
    channel = request.args.get('channel')
    if channel:
        q = q.filter(MessageTemplate.channel.in_([channel, 'any']))
    if year:
        q = q.filter(db.or_(MessageTemplate.pujan_year == year,
                            MessageTemplate.pujan_year.is_(None)))
    rows = q.order_by(MessageTemplate.purpose, MessageTemplate.language,
                      MessageTemplate.name).all()
    return jsonify({'rows': [r.to_dict() for r in rows],
                    'placeholders': [{'key': k, 'about': a} for k, a in COMM_PLACEHOLDERS],
                    'purposes': ['sankalp', 'pending', 'event', 'checkin', 'welcome', 'custom'],
                    'channels': ['any', 'sms', 'whatsapp', 'email']})


@app.route('/api/templates', methods=['POST'])
@perm_required('comm.templates')
def api_create_template():
    d = request.get_json(silent=True) or {}
    if not (d.get('name') or '').strip() or not (d.get('body') or '').strip():
        return jsonify({'error': 'A template needs a name and a message'}), 400
    tpl = MessageTemplate(
        name=d['name'].strip(), name_gu=(d.get('name_gu') or '').strip(),
        purpose=(d.get('purpose') or 'custom'),
        channel=(d.get('channel') or 'any'), language=(d.get('language') or DEFAULT_LANGUAGE),
        subject=(d.get('subject') or '').strip(), body=d['body'],
        include_poster=bool(d.get('include_poster')),
        pujan_year=d.get('pujan_year') or None)
    db.session.add(tpl); db.session.commit()
    audit('create', 'template', tpl, label=tpl.name)
    db.session.commit()
    return jsonify({'success': True, 'template': tpl.to_dict()})


@app.route('/api/templates/<int:tid>', methods=['PUT'])
@perm_required('comm.templates')
def api_update_template(tid):
    d = request.get_json(silent=True) or {}
    tpl = MessageTemplate.query.get_or_404(tid)
    before = {'name': tpl.name, 'body': tpl.body, 'subject': tpl.subject}
    for f in ('name', 'name_gu', 'purpose', 'channel', 'language', 'subject', 'body'):
        if f in d and d[f] is not None:
            setattr(tpl, f, d[f].strip() if isinstance(d[f], str) else d[f])
    if 'include_poster' in d:
        tpl.include_poster = bool(d['include_poster'])
    if 'is_active' in d:
        tpl.is_active = bool(d['is_active'])
    if 'pujan_year' in d:
        tpl.pujan_year = d['pujan_year'] or None
    if not (tpl.name or '').strip() or not (tpl.body or '').strip():
        return jsonify({'error': 'A template needs a name and a message'}), 400
    db.session.commit()
    audit('update', 'template', tpl, old=before,
          new={'name': tpl.name, 'body': tpl.body, 'subject': tpl.subject}, label=tpl.name)
    db.session.commit()
    return jsonify({'success': True, 'template': tpl.to_dict()})


@app.route('/api/templates/<int:tid>', methods=['DELETE'])
@perm_required('comm.templates_delete')
def api_delete_template(tid):
    tpl = MessageTemplate.query.get_or_404(tid)
    if tpl.is_system:
        return jsonify({'error': 'A built-in template cannot be deleted. '
                                 'Edit it, or switch it off.'}), 400
    audit('delete', 'template', tpl, old={'name': tpl.name}, label=tpl.name)
    db.session.delete(tpl); db.session.commit()
    return jsonify({'success': True})


@app.route('/api/communicate/preview', methods=['POST'])
@perm_required('notify.email', 'notify.whatsapp', 'notify.sms')
def api_comm_preview():
    """Who would get this, and what the first one would read."""
    d = request.get_json(silent=True) or {}
    year = int(d.get('pujan_year') or get_current_year())
    years = _comm_years(d, year)
    tpl = MessageTemplate.query.get(d.get('template_id') or 0)
    if not tpl:
        return jsonify({'error': 'Pick a template'}), 404
    members = _comm_audience(d, year, years)
    cfg = get_event_config(year)
    channel = (d.get('channel') or 'whatsapp').lower()
    sample = None
    reachable = 0
    for m in members:
        if channel == 'email':
            ok = bool(m.email)
        else:
            ok = bool(m.mobile)
        if ok:
            reachable += 1
        if sample is None:
            # The preview shows the channel's own wording, so what an admin
            # reads is what the member will get.
            ctx = comm_context(m, year, cfg, years, channel=channel)
            sample = {'member': m.name,
                      'to': (m.email if channel == 'email' else m.mobile) or '',
                      'subject': notify.render(tpl.subject or '', ctx),
                      'body': notify.render(tpl.body, ctx)}
    return jsonify({'total': len(members), 'reachable': reachable,
                    'without_contact': len(members) - reachable,
                    'channel': channel, 'sample': sample, 'years': years,
                    'poster': bool(tpl.include_poster and cfg and (cfg.poster_path or cfg.poster_data))})


def _comm_years(d, year):
    """The years a message covers. Empty selection means every year on record."""
    raw = d.get('years')
    if raw:
        try:
            years = sorted({int(x) for x in raw})
            if years:
                return years
        except (ValueError, TypeError):
            pass
    if d.get('all_years'):
        got = [r[0] for r in db.session.query(Sankalp.pujan_year).distinct().all()]
        return sorted(got) or [year]
    return [year]


def _comm_audience(d, year, years=None):
    """The members a bulk message is aimed at.

    Either an explicit list of ids, or everyone matching the usual filters -
    centre, and optionally only those with something outstanding.
    """
    cf = get_user_center_filter()
    ids = d.get('member_ids') or []
    q = Member.query.filter_by(is_active=True)
    if cf:
        q = q.filter_by(center_id=cf)
    elif d.get('center_id'):
        q = q.filter_by(center_id=int(d['center_id']))
    if ids:
        q = q.filter(Member.id.in_(ids))
    members = q.order_by(Member.name).all()

    scope = (d.get('scope') or 'all').lower()
    span = years or [year]
    if scope in ('with_sankalp', 'pending'):
        out = []
        for m in members:
            sks = Sankalp.query.filter(Sankalp.member_id == m.id,
                                       Sankalp.pujan_year.in_(span)).all()
            if not sks:
                continue
            if scope == 'pending':
                # Anything owing in any of the chosen years counts.
                total = sum(Decimal(str(x.amount)) for x in sks)
                got = sum(sum(Decimal(str(c.amount)) for c in x.collections) for x in sks)
                if total - got <= 0:
                    continue
            out.append(m)
        members = out
    return members


@app.route('/api/communicate', methods=['POST'])
@perm_required('notify.email', 'notify.whatsapp', 'notify.sms')
def api_communicate():
    """Send one template to many members, over one channel.

    Email goes out directly when SMTP is configured. WhatsApp comes back as
    wa.me links, because WhatsApp has no unattended send without a Business
    API account. SMS goes through the provider hook in notify.py. Every
    message is logged either way.
    """
    d = request.get_json(silent=True) or {}
    channel = (d.get('channel') or '').lower()
    if channel not in ('sms', 'whatsapp', 'email'):
        return jsonify({'error': 'Choose SMS, WhatsApp or Email'}), 400
    need = {'sms': 'notify.sms', 'whatsapp': 'notify.whatsapp', 'email': 'notify.email'}[channel]
    me = User.query.get(session['user_id'])
    if not has_perm(me, need):
        return jsonify({'error': f'You do not have permission to send {channel}'}), 403

    year = int(d.get('pujan_year') or get_current_year())
    years = _comm_years(d, year)
    tpl = MessageTemplate.query.get(d.get('template_id') or 0)
    if not tpl:
        return jsonify({'error': 'Pick a template'}), 404
    members = _comm_audience(d, year, years)
    if not members:
        return jsonify({'error': 'Nobody matches that selection'}), 400

    cfg = get_event_config(year)
    poster = event_poster(cfg, year) if (tpl.include_poster and cfg) else None
    items, logs, skipped = [], [], 0

    for m in members:
        ctx = comm_context(m, year, cfg, years, channel=channel)
        body = notify.render(tpl.body, ctx)
        subject = notify.render(tpl.subject or '', ctx)
        dest = (m.email or '') if channel == 'email' else (m.mobile or '')
        if not dest:
            skipped += 1
            continue
        log = CommLog(member_id=m.id, comm_type=channel, destination=dest,
                      subject=subject, message=body, status='queued',
                      pujan_year=year, template_id=tpl.id,
                      audience='ho' if m.attend_at_ho else 'center')
        db.session.add(log)
        logs.append(log)
        # One list feeds all three senders, so it carries both sets of
        # names: 'to'/'body' for email, 'phone'/'message' for SMS and
        # WhatsApp. The QR travels as bytes so the email can show it rather
        # than link to it.
        item = {'to': dest, 'body': body,
                'email': dest, 'phone': dest,
                'subject': subject, 'message': body}
        pas = Pass.query.filter_by(member_id=m.id, pujan_year=year).first()
        if pas:
            item['qr_png'] = qr_png(pas.pass_number)
            item['qr_name'] = f'{pas.pass_number}.png'
        items.append(item)
    db.session.flush()

    result = {'total': len(members), 'queued': len(logs), 'skipped': skipped,
              'channel': channel, 'template': tpl.name}

    if channel == 'email':
        if notify.email_configured():
            outcomes = notify.send_emails(items, attachment=poster)
            for log, res in zip(logs, outcomes):
                log.status = 'sent' if res.get('ok') else 'failed'
                log.error = res.get('error')
            result['sent'] = sum(1 for r in outcomes if r.get('ok'))
            result['failed'] = len(outcomes) - result['sent']
        else:
            result['note'] = 'No SMTP configured, so these are queued only.'
    elif channel == 'sms':
        if notify.sms_configured():
            outcomes = notify.send_sms_via_provider(items)
            for log, res in zip(logs, outcomes):
                log.status = 'sent' if res.get('ok') else 'failed'
                log.error = res.get('error')
            result['sent'] = sum(1 for r in outcomes if r.get('ok'))
            result['failed'] = len(outcomes) - result['sent']
        else:
            result['note'] = ('No SMS gateway configured. The messages are logged '
                              'and ready; set SMS_API_URL and SMS_API_KEY to send.')
    elif notify.whatsapp_configured():
        outcomes = notify.send_whatsapp_via_provider(items)
        for log, res in zip(logs, outcomes):
            log.status = 'sent' if res.get('ok') else 'failed'
            log.error = res.get('error')
        result['sent'] = sum(1 for r in outcomes if r.get('ok'))
        result['failed'] = len(outcomes) - result['sent']
    else:
        # No provider: one link per person, opened by the sender. Nothing is
        # lost, it just needs a phone in hand.
        result['links'] = [{'member': m.name, 'phone': it['phone'],
                            'url': notify.whatsapp_link(it['phone'], it['message'])}
                           for m, it in zip([x for x in members if (x.mobile or '')], items)]
        result['note'] = 'Open each link to send from your own WhatsApp.'

    db.session.commit()
    audit('create', 'communication', None,
          label=f'{tpl.name} - {channel} x {len(logs)}', year=year,
          new={'channel': channel, 'template': tpl.name, 'queued': len(logs)})
    db.session.commit()
    return jsonify(result)


def _parse_when(raw):
    """A date and time from the form, read as IST and stored as UTC."""
    if not raw:
        return None
    txt = str(raw).strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M', '%d-%m-%Y %H:%M', '%Y-%m-%d %H:%M:%S'):
        try:
            naive = datetime.strptime(txt, fmt)
            return naive.replace(tzinfo=IST).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


@app.route('/api/schedules')
@perm_required('comm.schedules')
def api_schedules():
    rows = ScheduledMessage.query.order_by(ScheduledMessage.next_run_at).all()
    return jsonify({'rows': [r.to_dict() for r in rows],
                    'frequencies': ['once', 'daily', 'weekly', 'monthly', 'yearly'],
                    'now': to_ist(utcnow(), '%d-%m-%Y %H:%M')})


@app.route('/api/schedules', methods=['POST'])
@perm_required('comm.schedules')
def api_create_schedule():
    d = request.get_json(silent=True) or {}
    channel = (d.get('channel') or '').lower()
    if channel not in ('sms', 'whatsapp', 'email'):
        return jsonify({'error': 'Choose SMS, WhatsApp or Email'}), 400
    if channel == 'whatsapp':
        # Worth being plain about rather than letting it fail quietly later.
        return jsonify({'error': 'WhatsApp cannot be scheduled: every message has '
                                 'to be opened and sent from a phone. Schedule SMS '
                                 'or email instead.'}), 400
    need = {'sms': 'notify.sms', 'email': 'notify.email'}[channel]
    me = User.query.get(session['user_id'])
    if not has_perm(me, need):
        return jsonify({'error': f'You do not have permission to send {channel}'}), 403
    tpl = MessageTemplate.query.get(d.get('template_id') or 0)
    if not tpl:
        return jsonify({'error': 'Pick a template'}), 404
    when = _parse_when(d.get('next_run_at'))
    if not when:
        return jsonify({'error': 'Give the date and time to send'}), 400
    freq = (d.get('frequency') or 'once').lower()
    if freq not in ('once', 'daily', 'weekly', 'monthly', 'yearly'):
        return jsonify({'error': 'Frequency must be once, daily, weekly, monthly or yearly'}), 400
    cf = get_user_center_filter()
    sched = ScheduledMessage(
        name=(d.get('name') or tpl.name).strip(),
        template_id=tpl.id, channel=channel,
        scope=(d.get('scope') or 'all'),
        years=json.dumps([int(x) for x in (d.get('years') or [])]),
        center_id=cf or d.get('center_id') or None,
        frequency=freq, next_run_at=when, created_by=me.id)
    db.session.add(sched)
    db.session.commit()
    audit('create', 'schedule', sched, label=sched.name)
    db.session.commit()
    return jsonify({'success': True, 'schedule': sched.to_dict()})


@app.route('/api/schedules/<int:sid>', methods=['PUT'])
@perm_required('comm.schedules')
def api_update_schedule(sid):
    d = request.get_json(silent=True) or {}
    sched = ScheduledMessage.query.get_or_404(sid)
    if 'is_active' in d:
        sched.is_active = bool(d['is_active'])
    if d.get('next_run_at'):
        when = _parse_when(d['next_run_at'])
        if not when:
            return jsonify({'error': 'That date and time could not be read'}), 400
        sched.next_run_at = when
    for f in ('name', 'scope', 'frequency'):
        if d.get(f):
            setattr(sched, f, d[f])
    if 'years' in d:
        sched.years = json.dumps([int(x) for x in (d.get('years') or [])])
    db.session.commit()
    return jsonify({'success': True, 'schedule': sched.to_dict()})


@app.route('/api/schedules/<int:sid>', methods=['DELETE'])
@perm_required('comm.schedules')
def api_delete_schedule(sid):
    sched = ScheduledMessage.query.get_or_404(sid)
    audit('delete', 'schedule', sched, old={'name': sched.name}, label=sched.name)
    db.session.delete(sched)
    db.session.commit()
    return jsonify({'success': True})


def external_url(endpoint, **values):
    """A link fit to put in a message.

    url_for(_external=True) takes the host from the request, so a message sent
    by an admin working at localhost:3000 goes out carrying localhost links -
    useless to the member who receives them, and the reason those links have
    kept turning up wrong. PUBLIC_BASE_URL wins whenever it is set, whatever
    host the admin happens to be on.
    """
    base = (os.environ.get('PUBLIC_BASE_URL') or '').strip().rstrip('/')
    if base:
        return base + url_for(endpoint, **values)
    return url_for(endpoint, _external=True, **values)


def public_base_url():
    """The address a member's mail client will be able to reach.

    A scheduled send has no request to take the host from, so it comes from
    PUBLIC_BASE_URL. Unset, the QR links in a scheduled message would point at
    localhost and show as broken images in the email - so it is worth setting
    on any host that sends mail.
    """
    base = (os.environ.get('PUBLIC_BASE_URL') or '').strip().rstrip('/')
    return base or 'http://localhost:3000'


def scheduler_request_context():
    """A request context for work that runs on a timer.

    comm_context() builds the QR link with url_for(..., _external=True), which
    needs a request to know the scheme and host. Called from the scheduler
    thread there is none, and Flask says so: "Working outside of request
    context" - which reads like a bug in the schedule rather than a missing
    host name. Standing up a context with the real public address fixes the
    links as well as the error.
    """
    return app.test_request_context(base_url=public_base_url())


def run_due_schedules():
    """Send whatever is due.

    Each row is claimed with a conditional UPDATE before anything is sent, so
    with several web workers only one of them picks up a given schedule.
    """
    now = utcnow()
    due = ScheduledMessage.query.filter(
        ScheduledMessage.is_active.is_(True),
        ScheduledMessage.next_run_at <= now).all()
    done = []
    for sched in due:
        # The time it was actually due. The claim below overwrites next_run_at
        # to stop a second worker taking it, so the real slot has to be kept or
        # a weekly schedule would drift by however long the send took.
        slot = as_utc(sched.next_run_at)
        claimed = db.session.query(ScheduledMessage).filter(
            ScheduledMessage.id == sched.id,
            ScheduledMessage.next_run_at <= now,
            ScheduledMessage.is_active.is_(True)).update(
                {'last_run_at': now, 'next_run_at': utcnow() + timedelta(minutes=5)},
                synchronize_session=False)
        db.session.commit()
        if not claimed:
            continue                       # another worker got there first
        try:
            outcome = _send_bulk(sched)
            sched = db.session.get(ScheduledMessage, sched.id)
            sched.run_count = (sched.run_count or 0) + 1
            sched.last_result = outcome
            nxt = advance_schedule(sched, slot)
            if nxt:
                sched.next_run_at = nxt
            else:
                sched.is_active = False    # a one-off is finished
            db.session.commit()
            done.append({'id': sched.id, 'name': sched.name, 'result': outcome})
        except Exception as e:
            db.session.rollback()
            sched = db.session.get(ScheduledMessage, sched.id)
            sched.last_result = f'{e.__class__.__name__}: {e}'
            sched.is_active = False
            db.session.commit()
            done.append({'id': sched.id, 'name': sched.name, 'result': sched.last_result})
    return done


def _send_bulk(sched):
    """One scheduled run. Returns a short line for the schedule's log."""
    year = get_current_year()
    years = sched.year_list or [year]
    tpl = sched.template
    cfg = get_event_config(year)
    poster = event_poster(cfg, year) if (tpl.include_poster and cfg) else None
    q = Member.query.filter_by(is_active=True)
    if sched.center_id:
        q = q.filter_by(center_id=sched.center_id)
    members = _filter_by_scope(q.order_by(Member.name).all(), sched.scope, years)

    items, logs, skipped = [], [], 0
    for m in members:
        ctx = comm_context(m, year, cfg, years, channel=sched.channel)
        body = notify.render(tpl.body, ctx)
        subject = notify.render(tpl.subject or '', ctx)
        dest = (m.email or '') if sched.channel == 'email' else (m.mobile or '')
        if not dest:
            skipped += 1
            continue
        log = CommLog(member_id=m.id, comm_type=sched.channel, destination=dest,
                      subject=subject, message=body, status='queued',
                      pujan_year=year, template_id=tpl.id,
                      audience='ho' if m.attend_at_ho else 'center')
        db.session.add(log)
        logs.append(log)
        # One list feeds all three senders, so it carries both sets of
        # names: 'to'/'body' for email, 'phone'/'message' for SMS and
        # WhatsApp. The QR travels as bytes so the email can show it rather
        # than link to it.
        item = {'to': dest, 'body': body,
                'email': dest, 'phone': dest,
                'subject': subject, 'message': body}
        pas = Pass.query.filter_by(member_id=m.id, pujan_year=year).first()
        if pas:
            item['qr_png'] = qr_png(pas.pass_number)
            item['qr_name'] = f'{pas.pass_number}.png'
        items.append(item)
    db.session.flush()

    sent = 0
    if sched.channel == 'email' and notify.email_configured():
        for log, res in zip(logs, notify.send_emails(items, attachment=poster)):
            log.status = 'sent' if res.get('ok') else 'failed'
            log.error = res.get('error')
            sent += 1 if res.get('ok') else 0
        note = f'{sent} sent, {len(logs) - sent} failed'
    elif sched.channel == 'whatsapp' and notify.whatsapp_configured():
        for log, res in zip(logs, notify.send_whatsapp_via_provider(items)):
            log.status = 'sent' if res.get('ok') else 'failed'
            log.error = res.get('error')
            sent += 1 if res.get('ok') else 0
        note = f'{sent} sent, {len(logs) - sent} failed'
    elif sched.channel == 'sms' and notify.sms_configured():
        for log, res in zip(logs, notify.send_sms_via_provider(items)):
            log.status = 'sent' if res.get('ok') else 'failed'
            log.error = res.get('error')
            sent += 1 if res.get('ok') else 0
        note = f'{sent} sent, {len(logs) - sent} failed'
    else:
        note = f'{len(logs)} queued (no {sched.channel} gateway configured)'
    db.session.commit()
    return f'{note}; {skipped} without a contact'


def _filter_by_scope(members, scope, years):
    if scope not in ('with_sankalp', 'pending'):
        return members
    out = []
    for m in members:
        sks = Sankalp.query.filter(Sankalp.member_id == m.id,
                                   Sankalp.pujan_year.in_(years)).all()
        if not sks:
            continue
        if scope == 'pending':
            total = sum(Decimal(str(x.amount)) for x in sks)
            got = sum(sum(Decimal(str(c.amount)) for c in x.collections) for x in sks)
            if total - got <= 0:
                continue
        out.append(m)
    return out


@app.route('/api/schedules/run-due', methods=['POST'])
@perm_required('notify.email', 'notify.sms')
def api_run_due():
    """Runs anything due right now, for when you would rather not wait."""
    return jsonify({'ran': run_due_schedules()})


@app.route('/api/notifications/preview')
@perm_required('notify.email', 'notify.whatsapp', 'event.config')
def api_notify_preview():
    year = request.args.get('year', get_current_year(), type=int)
    audience = request.args.get('audience', 'center')
    cfg = get_event_config(year)
    lang = request.args.get('lang') or cfg.language or DEFAULT_LANGUAGE
    members = _audience_members(audience, year, get_user_center_filter())
    sample = members[0] if members else None
    body = subject = ''
    if sample:
        ctx = _msg_context(cfg, sample, lang, audience, year)
        body = notify.render(_template_for(cfg, lang, audience), ctx)
        subject = notify.render(cfg.subject_gu if lang == 'gu' else cfg.subject_en, ctx)
    with_email = sum(1 for m in members if m.email)
    with_phone = sum(1 for m in members if notify.normalise_phone(m.mobile))
    return jsonify({
        'year': year, 'audience': audience, 'lang': lang,
        'sample_member': sample.name if sample else '',
        'subject': subject, 'body': body,
        'total': len(members), 'with_email': with_email, 'with_whatsapp': with_phone,
        'email_configured': notify.email_configured(),
        'has_poster': bool(cfg.poster_path or cfg.poster_data),
        'placeholders': notify.placeholders_in(_template_for(cfg, lang, audience)),
    })


@app.route('/api/notifications/send', methods=['POST'])
@perm_required('notify.email')
def api_notify_send():
    """Email only. WhatsApp has no unattended API without an approved
    provider, so that side hands back wa.me links instead of pretending."""
    data = request.get_json(silent=True) or {}
    year = int(data.get('year') or get_current_year())
    audience = data.get('audience', 'center')
    cfg = get_event_config(year)
    lang = data.get('lang') or cfg.language or DEFAULT_LANGUAGE
    if not notify.email_configured():
        return jsonify({'error': 'Email is not configured on the server. Set the '
                                 'SMTP_* variables in .env, or use the WhatsApp '
                                 'links instead.'}), 400
    members = _audience_members(audience, year, get_user_center_filter())
    only = data.get('member_ids')
    if only:
        keep = {int(i) for i in only}
        members = [m for m in members if m.id in keep]
    items, logs = [], {}
    for m in members:
        if not m.email:
            continue
        ctx = _msg_context(cfg, m, lang, audience, year)
        body = notify.render(_template_for(cfg, lang, audience), ctx)
        subject = notify.render(cfg.subject_gu if lang == 'gu' else cfg.subject_en, ctx)
        log = CommLog(member_id=m.id, comm_type='email', destination=m.email,
                      subject=subject, message=body, status='queued',
                      pujan_year=year, audience=audience)
        db.session.add(log)
        db.session.flush()
        logs[log.id] = log
        item = {'to': m.email, 'subject': subject, 'body': body, 'ref': log.id}
        pas = Pass.query.filter_by(member_id=m.id, pujan_year=year).first()
        if pas:
            item['qr_png'] = qr_png(pas.pass_number)
            item['qr_name'] = f'{pas.pass_number}.png'
        items.append(item)
    if not items:
        db.session.rollback()
        return jsonify({'error': 'None of those members have an email address'}), 400
    attachment = event_poster(cfg, year) if data.get('attach_poster', True) else None
    results = notify.send_emails(items, attachment=attachment)
    sent = failed = 0
    for r in results:
        log = logs.get(r.get('ref'))
        if not log:
            continue
        log.status = r['status']
        log.error = r.get('error')
        sent += 1 if r['ok'] else 0
        failed += 0 if r['ok'] else 1
    db.session.commit()
    return jsonify({'ok': failed == 0, 'sent': sent, 'failed': failed,
                    'skipped_no_email': len(members) - len(items),
                    'errors': [r.get('error') for r in results if not r['ok']][:10]})


@app.route('/api/notifications/whatsapp')
@perm_required('notify.whatsapp')
def api_notify_whatsapp():
    """One ready-to-send wa.me link per member."""
    year = request.args.get('year', get_current_year(), type=int)
    audience = request.args.get('audience', 'center')
    cfg = get_event_config(year)
    lang = request.args.get('lang') or cfg.language or DEFAULT_LANGUAGE
    members = _audience_members(audience, year, get_user_center_filter())
    out, no_phone = [], 0
    for m in members:
        ctx = _msg_context(cfg, m, lang, audience, year)
        body = notify.render(_template_for(cfg, lang, audience), ctx)
        link = notify.whatsapp_link(m.mobile, body)
        if not link:
            no_phone += 1
            continue
        out.append({'member_id': m.id, 'member_code': m.member_code or '',
                    'name': m.name, 'mobile': m.mobile,
                    'center_name': m.center.label if m.center else '', 'link': link})
    return jsonify({'year': year, 'audience': audience, 'lang': lang,
                    'links': out, 'total': len(out), 'no_phone': no_phone})


@app.route('/api/audit')
@perm_required('page.audit')
def api_audit():
    """The activity log. Admins see their own centre's users; a super admin
    sees everything."""
    user = User.query.get(session['user_id'])
    q = AuditLog.query
    if user.role == 'admin':
        q = q.filter(db.or_(AuditLog.center_id == user.center_id,
                            AuditLog.user_id == user.id))
    for field, arg in (('action', 'action'), ('entity_type', 'entity_type')):
        val = request.args.get(arg)
        if val:
            q = q.filter(getattr(AuditLog, field) == val)
    uid = request.args.get('user_id', type=int)
    if uid:
        q = q.filter(AuditLog.user_id == uid)
    year = request.args.get('year', type=int)
    if year:
        q = q.filter(AuditLog.pujan_year == year)
    frm, to = request.args.get('from'), request.args.get('to')
    try:
        if frm:
            q = q.filter(AuditLog.created_at >= datetime.combine(_parse_date(frm), datetime.min.time()))
        if to:
            q = q.filter(AuditLog.created_at <= datetime.combine(_parse_date(to), datetime.max.time()))
    except ValueError:
        return jsonify({'error': 'Dates must be dd-mm-yyyy'}), 400
    search = (request.args.get('search') or '').strip()
    if search:
        like = f'%{search}%'
        q = q.filter(db.or_(AuditLog.username.ilike(like), AuditLog.full_name.ilike(like),
                            AuditLog.entity_label.ilike(like), AuditLog.old_values.ilike(like),
                            AuditLog.new_values.ilike(like)))
    total = q.count()
    page = max(1, request.args.get('page', 1, type=int))
    per = min(200, max(1, request.args.get('per_page', 50, type=int)))
    rows = q.order_by(AuditLog.created_at.desc()).offset((page - 1) * per).limit(per).all()
    return jsonify({
        'rows': [r.to_dict() for r in rows], 'total': total,
        'page': page, 'per_page': per,
        'pages': max(1, (total + per - 1) // per),
        'users': [{'id': u.id, 'name': f'{u.full_name} ({u.username})'}
                  for u in User.query.order_by(User.full_name).all()],
        'actions': ['login', 'logout', 'login_failed', 'create', 'update', 'delete'],
        'entity_types': ['member', 'sankalp', 'collection', 'center', 'user', 'pass'],
    })


@app.route('/api/notifications/log')
@perm_required('comm.log')
def api_notify_log():
    year = request.args.get('year', get_current_year(), type=int)
    rows = CommLog.query.filter_by(pujan_year=year).order_by(
        CommLog.sent_at.desc()).limit(300).all()
    return jsonify([r.to_dict() for r in rows])


# ======================== CSV IMPORT / EXPORT ========================
def _truthy(v, default=True):
    v = str(v).strip().lower()
    if v in ('1', 'true', 'yes', 'y', 'active'):
        return True
    if v in ('0', 'false', 'no', 'n', 'inactive'):
        return False
    return default


def _center_by_name(name):
    """Match a centre on either language, case-insensitively."""
    n = (name or '').strip()
    if not n:
        return None
    return Center.query.filter(db.or_(
        db.func.lower(Center.name_en) == n.lower(),
        db.func.lower(Center.name) == n.lower())).first()


# ---------------------------- EXPORT ----------------------------
def _export_centers(cf):
    q = Center.query if not cf else Center.query.filter_by(id=cf)
    return [{'name_en': c.name_en or '', 'name_gu': c.name or '', 'city': c.city or '',
             'phone': c.phone or '', 'email': c.email or '',
             'is_active': 'yes' if c.is_active else 'no'}
            for c in q.order_by(Center.name_en).all()]


def _export_members(cf):
    """One row per firm/company, repeating the member's core fields.

    This is how the sheets are kept by hand, and the importer merges the rows
    back into one member. A member with no business gets a single blank-firm
    row so nobody disappears from the file.
    """
    q = Member.query.filter_by(is_active=True)
    if cf:
        q = q.filter_by(center_id=cf)
    out = []
    for m in q.order_by(Member.member_code).all():
        base = {
            'member_code': m.member_code or '', 'name': m.name,
            'member_type': m.member_type,
            'center': (m.center.name_en or m.center.name) if m.center else '',
            'mobile': m.mobile or '', 'email': m.email or '',
            'dharmada_type': m.dharmada_type or '',
            'dharmada_amount': (f'{float(m.dharmada_amount):.0f}'
                                if m.dharmada_amount else ''),
            'address': m.address or '',
        }
        firms, comps = m.firms, m.companies
        if not firms and not comps:
            out.append({**base, 'firms': '', 'companies': ''})
            continue
        for i in range(max(len(firms), len(comps))):
            out.append({**base,
                        'firms': firms[i].name if i < len(firms) else '',
                        'companies': comps[i].name if i < len(comps) else ''})
    return out


def _export_partners(cf):
    q = Member.query.filter_by(is_active=True)
    if cf:
        q = q.filter_by(center_id=cf)
    out = []
    for m in q.order_by(Member.member_code).all():
        for e in m.entities:
            people = e.people or [None]
            for p in people:
                out.append({
                    'member_code': m.member_code or '', 'entity_type': e.entity_type,
                    'entity_name': e.name, 'entity_email': e.email or '',
                    'entity_contact': e.contact or '', 'entity_address': e.address or '',
                    'entity_pan': e.pan_number or '',
                    'person_code': (p.member_code or '') if p else '',
                    'person_name': p.name if p else '',
                    'mobile': (p.mobile or '') if p else '',
                    'email': (p.email or '') if p else '',
                    'person_center': (p.center.name_en or p.center.name) if (p and p.center) else '',
                })
    return out


def _export_sankalps(cf, year=None):
    q = Sankalp.query
    if year:
        q = q.filter_by(pujan_year=year)
    if cf:
        q = q.filter_by(center_id=cf)
    out = []
    for sk in q.order_by(Sankalp.pujan_year.desc(), Sankalp.id).all():
        got = sum(float(c.amount) for c in sk.collections)
        out.append({
            'member_code': (sk.member.member_code or '') if sk.member else '',
            'member_name': sk.member.name if sk.member else '',
            'pujan_year': sk.pujan_year, 'sankalp_type': sk.sankalp_type,
            'firm_or_company': sk.entity.name if sk.entity else '',
            'partner_or_director': sk.person.name if sk.person else '',
            'sankalp_name': sk.sankalp_name, 'amount': float(sk.amount),
            'primary_center': (sk.owner_center.name_en or sk.owner_center.name) if sk.owner_center else '',
            'collecting_center': (sk.center.name_en or sk.center.name) if sk.center else '',
            'collected': got, 'pending': float(sk.amount) - got,
            'carried_from_year': sk.carried_from.pujan_year if sk.carried_from else '',
        })
    return out


def _export_collections(cf, year=None):
    q = Collection.query.join(Sankalp, Collection.sankalp_id == Sankalp.id)
    if year:
        q = q.filter(Sankalp.pujan_year == year)
    if cf:
        q = q.filter(Collection.center_id == cf)
    out = []
    for c in q.order_by(Collection.collection_date.desc()).all():
        sk = db.session.get(Sankalp, c.sankalp_id)
        out.append({
            'member_code': (sk.member.member_code or '') if sk and sk.member else '',
            'pujan_year': sk.pujan_year if sk else '',
            'sankalp_name': sk.sankalp_name if sk else '',
            'collection_date': c.collection_date.strftime('%Y-%m-%d') if c.collection_date else '',
            'amount': float(c.amount),
            'center': (c.center.name_en or c.center.name) if c.center else '',
            'remarks': c.remarks or '',
        })
    return out


def _export_users(cf):
    q = User.query
    if cf:
        q = q.filter_by(center_id=cf)
    return [{'username': u.username, 'full_name': u.full_name, 'role': u.role,
             'center': (u.center.name_en or u.center.name) if u.center else '',
             'mobile': u.mobile or '', 'email': u.email or '',
             'language': u.language or 'en',
             'is_active': 'yes' if u.is_active else 'no',
             'password': ''}              # never exported, only importable
            for u in q.order_by(User.username).all()]


def _imp_users(rows, errors):
    made = upd = 0
    valid_roles = tuple(r.key for r in Role.query.filter_by(is_active=True).all()) \
        or ('super_admin', 'admin', 'center_sant', 'center_user')
    for i, r in enumerate(rows, start=2):
        uname = csvio._s(r, 'username').lower()
        if not uname:
            errors.append({'row': i, 'message': 'username is required'}); continue
        role = csvio._s(r, 'role', 'center_user').lower()
        if role not in valid_roles:
            errors.append({'row': i, 'message': f'role must be one of {", ".join(valid_roles)}'}); continue
        center = _center_by_name(csvio._s(r, 'center'))
        pwd = csvio._s(r, 'password')
        u = User.query.filter_by(username=uname).first()
        if not u:
            if not pwd:
                errors.append({'row': i, 'message': f'password is required for the new user "{uname}"'}); continue
            if role != 'super_admin' and not center:
                errors.append({'row': i, 'message': f'center "{csvio._s(r, "center")}" not found'}); continue
            u = User(username=uname); db.session.add(u); made += 1
        else:
            upd += 1
        u.full_name = csvio._s(r, 'full_name') or uname
        u.role = role
        if center:
            u.center_id = center.id
        u.mobile = csvio._s(r, 'mobile'); u.email = csvio._s(r, 'email')
        u.language = csvio._s(r, 'language', 'en') or 'en'
        u.is_active = _truthy(csvio._s(r, 'is_active', 'yes'))
        if pwd:                       # only ever set, never read back out
            u.set_password(pwd)
        db.session.flush()
    return made, upd


EXPORTERS = {'centers': _export_centers, 'members': _export_members, 'users': _export_users,
             'partners': _export_partners, 'sankalps': _export_sankalps,
             'collections': _export_collections}


@app.route('/api/export/<kind>')
@login_required
def api_export(kind):
    spec = csvio.SPECS.get(kind)
    if not spec:
        return jsonify({'error': 'Unknown export'}), 404
    if kind == 'users' and not has_perm(User.query.get(session['user_id']), 'user.manage'):
        return jsonify({'error': 'You do not have permission to export users'}), 403
    cf = get_user_center_filter()
    year = request.args.get('year', type=int)
    fn = EXPORTERS[kind]
    rows = fn(cf, year) if kind in ('sankalps', 'collections') else fn(cf)
    if request.args.get('template') == '1':
        rows = []
    body = csvio.write_csv(spec['headers'], rows)
    stamp = datetime.now(IST).strftime('%Y%m%d')
    name = f"smvs_{kind}{'_template' if request.args.get('template') == '1' else ''}_{stamp}.csv"
    return Response(body, mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})


# ---------------------------- IMPORT ----------------------------
def _imp_centers(rows, errors):
    made = upd = 0
    for i, r in enumerate(rows, start=2):
        name_en = csvio._s(r, 'name_en')
        name_gu = csvio._s(r, 'name_gu')
        if not name_en and not name_gu:
            errors.append({'row': i, 'message': 'name_en is required'}); continue
        c = _center_by_name(name_en) or _center_by_name(name_gu)
        if not c:
            c = Center(); db.session.add(c); made += 1
        else:
            upd += 1
        c.name_en = name_en or name_gu
        c.name = name_gu or name_en
        c.city = csvio._s(r, 'city'); c.phone = csvio._s(r, 'phone')
        c.email = csvio._s(r, 'email')
        c.is_active = _truthy(csvio._s(r, 'is_active', 'yes'))
        db.session.flush()
    return made, upd


def _imp_members(rows, errors):
    """One member per member_code, however many rows that takes.

    A repeated member_code is NOT an error. Real exports put one firm or
    company per line and repeat the code, so 435592 with three firms is three
    rows. Those rows are merged into one member holding three firms. Only a
    genuine conflict is reported: the same code carrying two different names,
    which means two different people were given the same ID.
    """
    made = upd = ents = 0
    groups = {}                      # code -> [(row_number, row), ...]
    order = []
    for i, r in enumerate(rows, start=2):
        code = csvio._s(r, 'member_code').upper()
        if not code:
            errors.append({'row': i, 'message': 'member_code is required'})
            continue
        if code not in groups:
            groups[code] = []
            order.append(code)
        groups[code].append((i, r))

    for code in order:
        entries = groups[code]
        first_row, first = entries[0]

        # The name must agree across every row for this code.
        names = {csvio._s(r, 'name') for _, r in entries if csvio._s(r, 'name')}
        if not names:
            errors.append({'row': first_row, 'message': 'name is required'})
            continue
        if len(names) > 1:
            errors.append({'row': first_row, 'message':
                f'member_code "{code}" is used for different people: '
                + ' / '.join(sorted(names)[:3])
                + '. Give each person their own Member ID.'})
            continue
        name = names.pop()

        mtype = (csvio._s(first, 'member_type', 'individual') or 'individual').lower()
        if mtype not in MEMBER_TYPES:
            errors.append({'row': first_row, 'message':
                f'member_type must be one of {", ".join(MEMBER_TYPES)}'})
            continue
        center_raw = next((csvio._s(r, 'center') for _, r in entries if csvio._s(r, 'center')), '')
        center = _center_by_name(center_raw)

        m = Member.query.filter_by(member_code=code).first()
        if not m:
            if not center:
                errors.append({'row': first_row, 'message':
                    f'center "{center_raw}" not found' if center_raw else 'center is required'})
                continue
            m = Member(member_code=code, center_id=center.id)
            db.session.add(m)
            made += 1
        else:
            if center:
                m.center_id = center.id
            upd += 1

        m.name = name
        m.member_type = mtype
        m.is_active = True
        # Take the first non-blank value across the group for each field, so a
        # detail filled in on only one of the rows is not lost.
        for field, col in (('mobile', 'mobile'), ('email', 'email'),
                           ('address', 'address'), ('dharmada_type', 'dharmada_type')):
            val = next((csvio._s(r, col) for _, r in entries if csvio._s(r, col)), '')
            if val:
                setattr(m, field, val)
        # Dharmada is a number, so it goes through the money parser.
        dv = next((csvio._s(r, 'dharmada_amount') for _, r in entries
                   if csvio._s(r, 'dharmada_amount')), '')
        if dv:
            m.dharmada_amount = _money(dv)
        db.session.flush()

        # Every firm and company named anywhere in the group. Repeating the
        # same one is ignored rather than reported - re-importing a file must
        # not create duplicates.
        for _, r in entries:
            for col, etype in (('firms', 'firm'), ('companies', 'company')):
                raw = csvio._s(r, col)
                for nm in [x.strip() for x in raw.replace(';', '|').split('|') if x.strip()]:
                    if any(e.entity_type == etype and e.name.lower() == nm.lower()
                           for e in m.entities):
                        continue
                    m.entities.append(Entity(entity_type=etype, name=nm))
                    ents += 1
        db.session.flush()

    if ents:
        print(f"[import] {ents} firm/company row(s) attached")
    return made, upd


def _imp_partners(rows, errors):
    made = upd = 0
    for i, r in enumerate(rows, start=2):
        code = csvio._s(r, 'member_code').upper()
        etype = csvio._s(r, 'entity_type').lower()
        ename = csvio._s(r, 'entity_name')
        if not code or not ename:
            errors.append({'row': i, 'message': 'member_code and entity_name are required'}); continue
        if etype not in ('firm', 'company'):
            errors.append({'row': i, 'message': 'entity_type must be firm or company'}); continue
        m = Member.query.filter_by(member_code=code).first()
        if not m:
            errors.append({'row': i, 'message': f'no member with ID "{code}"'}); continue
        ent = next((e for e in m.entities
                    if e.entity_type == etype and e.name.lower() == ename.lower()), None)
        if not ent:
            ent = Entity(entity_type=etype, name=ename); m.entities.append(ent); made += 1
        else:
            upd += 1
        for attr, col in (('email', 'entity_email'), ('contact', 'entity_contact'),
                          ('address', 'entity_address'), ('pan_number', 'entity_pan')):
            val = csvio._s(r, col)
            if val:
                setattr(ent, attr, val)
        db.session.flush()
        pname = csvio._s(r, 'person_name')
        if not pname:
            continue
        pcenter = _center_by_name(csvio._s(r, 'person_center'))
        if csvio._s(r, 'person_center') and not pcenter:
            errors.append({'row': i, 'message': f'person_center "{csvio._s(r, "person_center")}" not found'}); continue
        p = next((x for x in ent.people if x.name.lower() == pname.lower()), None)
        if not p:
            p = EntityPerson(name=pname); ent.people.append(p)
        pcode = csvio._s(r, 'person_code').upper()
        if not pcode and not p.member_code:
            errors.append({'row': i, 'message': f'person_code (Member ID) is required for "{pname}"'}); continue
        if pcode:
            p.member_code = pcode
        # Fixed by the entity type, not taken from the file.
        p.designation = 'Director' if etype == 'company' else 'Partner'
        p.mobile = csvio._s(r, 'mobile') or p.mobile
        p.email = csvio._s(r, 'email') or p.email
        p.center_id = pcenter.id if pcenter else p.center_id
        db.session.flush()
    return made, upd


def _imp_sankalps(rows, errors):
    made = upd = 0
    for i, r in enumerate(rows, start=2):
        code = csvio._s(r, 'member_code').upper()
        m = Member.query.filter_by(member_code=code).first()
        if not m:
            errors.append({'row': i, 'message': f'no member with ID "{code}"'}); continue
        try:
            year = csvio._year(csvio._s(r, 'pujan_year'), 'pujan_year')
            amount = csvio._decimal(csvio._s(r, 'amount'), 'amount')
        except ValueError as e:
            errors.append({'row': i, 'message': str(e)}); continue
        stype = csvio._s(r, 'sankalp_type', 'individual').lower()
        if stype not in ('individual', 'firm', 'firm_partner', 'company', 'company_director'):
            errors.append({'row': i, 'message': f'unknown sankalp_type "{stype}"'}); continue
        ent = per = None
        if stype != 'individual':
            wants = 'firm' if stype in ('firm', 'firm_partner') else 'company'
            ename = csvio._s(r, 'firm_or_company')
            pool = [e for e in m.entities if e.entity_type == wants]
            ent = next((e for e in pool if e.name.lower() == ename.lower()), None) if ename else (pool[0] if len(pool) == 1 else None)
            if not ent:
                errors.append({'row': i, 'message': f'firm_or_company "{ename}" not found for {code}'}); continue
            if stype in ('firm_partner', 'company_director'):
                pname = csvio._s(r, 'partner_or_director')
                per = next((p for p in ent.people if p.name.lower() == pname.lower()), None) if pname else (ent.people[0] if len(ent.people) == 1 else None)
                if not per:
                    errors.append({'row': i, 'message': f'partner_or_director "{pname}" not found in {ent.name}'}); continue
        collect_cid = (per.effective_center_id if per else m.center_id) or m.center_id
        sk = Sankalp.query.filter_by(member_id=m.id, pujan_year=year, sankalp_type=stype,
                                     entity_id=ent.id if ent else None,
                                     person_id=per.id if per else None).first()
        if not sk:
            sk = Sankalp(member_id=m.id, pujan_year=year, sankalp_type=stype,
                         entity_id=ent.id if ent else None,
                         person_id=per.id if per else None,
                         created_by=session.get('user_id'))
            db.session.add(sk); made += 1
        else:
            upd += 1
        sk.sankalp_name = csvio._s(r, 'sankalp_name') or (ent.name if ent else m.name)
        sk.amount = amount
        sk.center_id = collect_cid
        sk.owner_center_id = m.center_id
        db.session.flush()
    return made, upd


def _imp_collections(rows, errors):
    made = 0
    for i, r in enumerate(rows, start=2):
        code = csvio._s(r, 'member_code').upper()
        m = Member.query.filter_by(member_code=code).first()
        if not m:
            errors.append({'row': i, 'message': f'no member with ID "{code}"'}); continue
        try:
            year = csvio._year(csvio._s(r, 'pujan_year'), 'pujan_year')
            amount = csvio._decimal(csvio._s(r, 'amount'), 'amount')
            cdate = csvio._date(csvio._s(r, 'collection_date'), 'collection_date')
        except ValueError as e:
            errors.append({'row': i, 'message': str(e)}); continue
        if amount <= 0:
            errors.append({'row': i, 'message': 'amount must be greater than zero'}); continue
        name = csvio._s(r, 'sankalp_name')
        pool = Sankalp.query.filter_by(member_id=m.id, pujan_year=year).all()
        sk = next((x for x in pool if x.sankalp_name.lower() == name.lower()), None) if name else (pool[0] if len(pool) == 1 else None)
        if not sk:
            errors.append({'row': i, 'message':
                f'no {year} sankalp named "{name}" for {code}'
                + (' (that member has several - name it exactly)' if len(pool) > 1 else '')}); continue
        already = sum(float(c.amount) for c in sk.collections)
        if already + float(amount) > float(sk.amount) + 0.001:
            errors.append({'row': i, 'message':
                f'collections would exceed the pledge ({already:.0f} + {float(amount):.0f} > {float(sk.amount):.0f})'}); continue
        # Same date + same amount on the same pledge is treated as already
        # imported, so re-running a file does not double the money.
        if any(c.collection_date == cdate and float(c.amount) == float(amount) for c in sk.collections):
            continue
        db.session.add(Collection(sankalp_id=sk.id, center_id=sk.center_id,
                                  amount=amount, collection_date=cdate,
                                  remarks=csvio._s(r, 'remarks'),
                                  created_by=session.get('user_id')))
        made += 1
        db.session.flush()
    return made, 0


IMPORTERS = {'centers': _imp_centers, 'members': _imp_members, 'users': _imp_users,
             'partners': _imp_partners, 'sankalps': _imp_sankalps,
             'collections': _imp_collections}


@app.route('/api/import/<kind>', methods=['POST'])
@perm_required('data.import')
def api_import(kind):
    spec = csvio.SPECS.get(kind)
    if not spec or kind not in IMPORTERS:
        return jsonify({'error': 'Unknown import'}), 404
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'Choose a CSV file first'}), 400
    try:
        headers, rows = csvio.read_csv(f.read())
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if not rows:
        return jsonify({'error': 'The file has no data rows'}), 400
    # The file says what it is. Picking the partners export in the Members box
    # used to fail with a baffling "Missing required column(s): name"; now the
    # right importer is chosen and the switch is reported back.
    detected = csvio.detect_kind(headers)
    switched = None
    if detected and detected != kind:
        switched = {'from': csvio.SPECS[kind]['label'], 'to': csvio.SPECS[detected]['label']}
        kind, spec = detected, csvio.SPECS[detected]
    elif not detected:
        return jsonify({'error': 'This file does not match any import format. Its columns are: '
                                 + ', '.join(headers[:8])
                                 + '. Download a blank template to see what is expected.'}), 400
    if kind == 'users' and not has_perm(User.query.get(session['user_id']), 'user.manage'):
        return jsonify({'error': 'You do not have permission to import users'}), 403

    commit = request.form.get('mode') == 'commit'
    errors = []
    try:
        created, updated = IMPORTERS[kind](rows, errors)
        if errors or not commit:
            # Validate mode, or a file with problems: change nothing at all. A
            # half-imported file is worse than no import.
            db.session.rollback()
            return jsonify({'ok': not errors, 'dry_run': True, 'rows': len(rows),
                            'kind': kind, 'switched': switched,
                            'created': created, 'updated': updated,
                            'errors': errors[:100], 'error_count': len(errors)})
        db.session.commit()
        return jsonify({'ok': True, 'dry_run': False, 'rows': len(rows),
                        'kind': kind, 'switched': switched,
                        'created': created, 'updated': updated, 'errors': []})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': f'{e.__class__.__name__}: {e}'}), 400


# ======================== DATABASE INIT ========================
DEFAULT_CENTERS = [
    ('Surat Center', 'Surat Center', 'Surat'),
    ('Vadodara Center', 'Vadodara Center', 'Vadodara'),
    ('Rajkot Center', 'Rajkot Center', 'Rajkot'),
    ('Bhavnagar Center', 'Bhavnagar Center', 'Bhavnagar'),
    ('Jamnagar Center', 'Jamnagar Center', 'Jamnagar'),
]


def legacy_alters_enabled():
    """Whether to run the hand-written ALTER list.

    On by default, because an un-stamped database still depends on it. Set
    LEGACY_ALTERS=0 once `flask db stamp head` has been run and migrations are
    in charge - then the only thing that changes the schema is Alembic, which is
    the point of adding it.
    """
    return os.environ.get('LEGACY_ALTERS', '1') == '1'


def alembic_state():
    """Which migration the database thinks it is on, if any."""
    try:
        row = db.session.execute(
            db.text('SELECT version_num FROM alembic_version')).first()
        return row[0] if row else None
    except Exception:
        db.session.rollback()
        return None


def init_db():
    """Create tables and, on a fresh database only, insert the starting data.

    Safe to run on every container boot.
    """
    with app.app_context():
        db.create_all()

        # Columns added after the first release, applied idempotently.
        #
        # Alembic is now wired up, but this list still runs because an existing
        # database has no alembic_version row and so Alembic cannot tell which
        # of these it already has. Once a deployment has been stamped - see
        # MIGRATIONS.md - set LEGACY_ALTERS=0 and Alembic alone owns the schema.
        state = alembic_state()
        if state:
            print(f"[init] Alembic revision: {state}")
        run_alters = legacy_alters_enabled()
        if not run_alters:
            print("[init] Legacy ALTERs off - schema changes come from migrations")
        for stmt in ([] if not run_alters else [
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS firm_address TEXT DEFAULT ''",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS firm_contact VARCHAR(20) DEFAULT ''",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS company_address TEXT DEFAULT ''",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS company_contact VARCHAR(20) DEFAULT ''",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS pan_number VARCHAR(20) DEFAULT ''",
        ]):
            try:
                db.session.execute(db.text(stmt))
                db.session.commit()
            except Exception as e:                    # one bad statement must not
                db.session.rollback()                 # abort the rest
                print(f"[init] skipped: {stmt.split('EXISTS')[-1].strip()} ({e.__class__.__name__})")

        for stmt in ([] if not run_alters else [
            "ALTER TABLE centers ADD COLUMN IF NOT EXISTS email VARCHAR(200) DEFAULT ''",
            "ALTER TABLE entity_persons ADD COLUMN IF NOT EXISTS center_id INTEGER REFERENCES centers(id)",
            "ALTER TABLE sankalps ADD COLUMN IF NOT EXISTS entity_id INTEGER REFERENCES entities(id)",
            "ALTER TABLE sankalps ADD COLUMN IF NOT EXISTS person_id INTEGER REFERENCES entity_persons(id)",
            "ALTER TABLE sankalps ADD COLUMN IF NOT EXISTS owner_center_id INTEGER REFERENCES centers(id)",
            "UPDATE sankalps SET owner_center_id = center_id WHERE owner_center_id IS NULL",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS member_code VARCHAR(50)",
            "ALTER TABLE entity_persons ADD COLUMN IF NOT EXISTS member_code VARCHAR(50)",
            "ALTER TABLE sankalps ADD COLUMN IF NOT EXISTS carried_from_id INTEGER REFERENCES sankalps(id)",
            # Partners created before this release have no Member ID; seed one
            # from the owning member so the column is never blank.
            "UPDATE entity_persons SET member_code = 'P' || id WHERE member_code IS NULL OR member_code = ''",
            "ALTER TABLE pass_config ADD COLUMN IF NOT EXISTS guruji_start INTEGER DEFAULT 1001",
            "ALTER TABLE pass_config ADD COLUMN IF NOT EXISTS guruji_prefix VARCHAR(10) DEFAULT 'GU'",
            "UPDATE pass_config SET guruji_start = 1001 WHERE guruji_start IS NULL",
            "UPDATE pass_config SET guruji_prefix = 'GU' WHERE guruji_prefix IS NULL OR guruji_prefix = ''",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS attend_at_ho BOOLEAN DEFAULT FALSE",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS ho_year INTEGER",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS ho_marked_by INTEGER REFERENCES users(id)",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS ho_marked_at TIMESTAMP",
            "UPDATE members SET attend_at_ho = FALSE WHERE attend_at_ho IS NULL",
            "ALTER TABLE comm_logs ADD COLUMN IF NOT EXISTS error TEXT",
            "ALTER TABLE comm_logs ADD COLUMN IF NOT EXISTS pujan_year INTEGER",
            "ALTER TABLE comm_logs ADD COLUMN IF NOT EXISTS audience VARCHAR(20)",
            "CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_log (created_at DESC)",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS permissions TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS user_type VARCHAR(20)",
            "ALTER TABLE message_templates ADD COLUMN IF NOT EXISTS is_system BOOLEAN DEFAULT FALSE",
            "ALTER TABLE instructions ADD COLUMN IF NOT EXISTS pujan_year INTEGER",
            "ALTER TABLE message_templates ADD COLUMN IF NOT EXISTS name_gu VARCHAR(150) DEFAULT ''",
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS seat_no INTEGER",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS dharmada_type VARCHAR(10) DEFAULT ''",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS event_registered_year INTEGER",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMP",
            "ALTER TABLE members ADD COLUMN IF NOT EXISTS dharmada_amount NUMERIC(12,2) DEFAULT 0",
            "ALTER TABLE passes ADD COLUMN IF NOT EXISTS print_count INTEGER DEFAULT 0",
            "ALTER TABLE sankalps ADD COLUMN IF NOT EXISTS verified_at TIMESTAMP",
            "ALTER TABLE comm_logs ADD COLUMN IF NOT EXISTS template_id INTEGER",
            "ALTER TABLE sankalps ADD COLUMN IF NOT EXISTS verified_by INTEGER",
            "ALTER TABLE passes ADD COLUMN IF NOT EXISTS last_printed_at TIMESTAMP",
            "ALTER TABLE passes ADD COLUMN IF NOT EXISTS last_printed_by INTEGER",
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS darshan_at TIMESTAMP",
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS darshan_by INTEGER",
            "ALTER TABLE manuals ADD COLUMN IF NOT EXISTS filename_gu VARCHAR(200) DEFAULT ''",
            "ALTER TABLE manuals ADD COLUMN IF NOT EXISTS data_gu BYTEA",
            "ALTER TABLE manuals ADD COLUMN IF NOT EXISTS file_path VARCHAR(500) DEFAULT ''",
            "ALTER TABLE manuals ADD COLUMN IF NOT EXISTS file_path_gu VARCHAR(500) DEFAULT ''",
            "ALTER TABLE event_config ADD COLUMN IF NOT EXISTS poster_path VARCHAR(500) DEFAULT ''",
            "ALTER TABLE manuals ADD COLUMN IF NOT EXISTS size_gu INTEGER DEFAULT 0",
            "ALTER TABLE manuals ADD COLUMN IF NOT EXISTS version_gu VARCHAR(40) DEFAULT ''",
            "ALTER TABLE manuals ADD COLUMN IF NOT EXISTS speech_en TEXT",
            "ALTER TABLE manuals ADD COLUMN IF NOT EXISTS speech_gu TEXT",
            "ALTER TABLE event_config ADD COLUMN IF NOT EXISTS selfserve_open BOOLEAN DEFAULT TRUE",
            "ALTER TABLE event_config ADD COLUMN IF NOT EXISTS selfserve_until DATE",
            "ALTER TABLE event_config ADD COLUMN IF NOT EXISTS selfserve_auto_add BOOLEAN DEFAULT TRUE",
            "ALTER TABLE event_config ADD COLUMN IF NOT EXISTS selfserve_auto_edit BOOLEAN DEFAULT TRUE",
            "ALTER TABLE event_config ADD COLUMN IF NOT EXISTS selfserve_auto_email BOOLEAN DEFAULT TRUE",
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS darshan_done_at TIMESTAMP",
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS darshan_done_by INTEGER",
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS slip_printed_at TIMESTAMP",
            "ALTER TABLE attendance ADD COLUMN IF NOT EXISTS slip_printed_by INTEGER",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_attendance_seat_year "
            "ON attendance (pujan_year, seat_no)",
        ]):
            try:
                db.session.execute(db.text(stmt)); db.session.commit()
            except Exception:
                db.session.rollback()

        # Drop the old global-unique index on pass_number if an earlier build
        # created it; passes are now unique per (year, number).
        try:
            db.session.execute(db.text("ALTER TABLE passes DROP CONSTRAINT IF EXISTS passes_pass_number_key"))
            db.session.commit()
        except Exception:
            db.session.rollback()

        # ---- member_type rename + firm/company backfill -------------------
        # 'firm_partner'/'company_director' described the PERSON. The types now
        # describe the ENTITY ('firm'/'company'), which can hold many partners
        # or directors, so the old single columns move into the entities table.
        try:
            migrated = 0
            legacy = Member.query.filter(
                Member.member_type.in_(['firm_partner', 'company_director'])).all()
            for m in legacy:
                if m.member_type == 'firm_partner':
                    m.member_type = 'firm'
                    src_name, src_addr, src_contact, etype = (
                        m.firm_name, m.firm_address, m.firm_contact, 'firm')
                else:
                    m.member_type = 'company'
                    src_name, src_addr, src_contact, etype = (
                        m.company_name, m.company_address, m.company_contact, 'company')
                if src_name and not m.entities:
                    ent = Entity(entity_type=etype, name=src_name,
                                 address=src_addr or '', contact=src_contact or '')
                    # The member themselves was the partner/director on record.
                    ent.people.append(EntityPerson(
                        name=m.name, mobile=m.mobile or '', email=m.email or '',
                        pan_number=m.pan_number or '',
                        designation=m.designation or ent.role_label))
                    m.entities.append(ent)
                migrated += 1
            if migrated:
                db.session.commit()
                print(f"[init] Migrated {migrated} member(s) to the firm/company model")
        except Exception as e:
            db.session.rollback()
            print(f"[init] member_type migration skipped: {e.__class__.__name__}")

        # Older configs used YJ from 1 and GN from 501, which overlaps the new
        # Guruji range. Move them onto the new starts, but only where no pass
        # of that type has been issued yet - never renumber an issued pass.
        try:
            for cfg in PassConfig.query.all():
                for ptype, field, want in (('yajman', 'yajman_start', 501),
                                           ('guruji', 'guruji_start', 1001),
                                           ('general', 'general_start', 1501)):
                    if Pass.query.filter_by(pujan_year=cfg.pujan_year,
                                            pass_type=ptype).count():
                        continue
                    if getattr(cfg, field, None) != want:
                        setattr(cfg, field, want)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"[init] pass range update skipped: {e.__class__.__name__}")

        # The Communication page used to be one right. Split into four, so
        # anyone who had the page keeps all of them.
        try:
            n = 0
            for r in Role.query.all():
                if r.key == 'super_admin':
                    continue
                pm, changed = r.perm_map, False
                if pm.get('page.communication'):
                    for k in ('comm.templates_view', 'comm.templates',
                              'comm.templates_delete', 'comm.schedules', 'comm.log'):
                        if not pm.get(k):
                            pm[k] = True; changed = True
                if changed:
                    r.permissions = json.dumps(pm); n += 1
            if n:
                db.session.commit()
                print(f"[init] Granted the communication rights to {n} role(s)")
        except Exception as e:
            db.session.rollback()
            print(f"[init] communication backfill skipped: {e.__class__.__name__}")

        # Say so at boot if the data already holds duplicates. The unique
        # constraints stop new ones, but rows entered before they existed
        # would slip through unnoticed otherwise.
        try:
            y = get_current_year()
            dup_seat = (db.session.query(Attendance.seat_no)
                        .filter(Attendance.pujan_year == y,
                                Attendance.seat_no.isnot(None))
                        .group_by(Attendance.seat_no)
                        .having(db.func.count(Attendance.id) > 1).count())
            dup_pass = (db.session.query(Pass.pass_number)
                        .filter(Pass.pujan_year == y)
                        .group_by(Pass.pass_number)
                        .having(db.func.count(Pass.id) > 1).count())
            if dup_seat or dup_pass:
                print(f"[init] WARNING: {y} has {dup_seat} duplicate seat number(s) "
                      f"and {dup_pass} duplicate pass number(s) from before the "
                      f"unique constraints. Open Activity Log > integrity, or "
                      f"GET /api/integrity, for the list.")
            missing = [k for k, v in unique_indexes_present().items() if v is False]
            if missing:
                print(f"[init] WARNING: these unique constraints are NOT on the "
                      f"tables: {', '.join(missing)}. Duplicates are possible "
                      f"until they are.")
        except Exception as e:
            print(f"[init] integrity check skipped: {e.__class__.__name__}")

        # Seeing money and bulk editing are new rights. Anyone who could see
        # the Sankalp page could already see the amounts, and anyone who could
        # edit a sankalp could already change one.
        try:
            n = 0
            for r in Role.query.all():
                if r.key == 'super_admin':
                    continue
                pm, changed = r.perm_map, False
                # finance.view was one switch for every figure. Anyone who had
                # it keeps all four of the new ones, and so does anyone who
                # could already open the Sankalp or Collection page.
                try:
                    raw = json.loads(r.permissions) if r.permissions else {}
                except (ValueError, TypeError):
                    raw = {}
                had_all = (bool(raw.get('finance.view')) or pm.get('page.sankalps')
                           or pm.get('page.collections'))
                for k in ('finance.sankalp', 'finance.collected',
                          'finance.pending', 'finance.dharmada'):
                    if had_all and not pm.get(k):
                        pm[k] = True; changed = True
                if pm.get('sankalp.edit') and not pm.get('sankalp.bulk_edit'):
                    pm['sankalp.bulk_edit'] = True; changed = True
                if changed:
                    r.permissions = json.dumps(pm); n += 1
            if n:
                db.session.commit()
                print(f"[init] Granted the money / bulk-edit rights to {n} role(s)")
        except Exception as e:
            db.session.rollback()
            print(f"[init] finance backfill skipped: {e.__class__.__name__}")

        # Seeing the attendance list is now its own right. Anyone who already
        # had the Attendance page could already see it, so keep that true.
        try:
            n = 0
            for r in Role.query.all():
                if r.key == 'super_admin':
                    continue
                pm = r.perm_map
                if pm.get('page.attendance') and not pm.get('attendance.list'):
                    pm['attendance.list'] = True
                    r.permissions = json.dumps(pm)
                    n += 1
            if n:
                db.session.commit()
                print(f"[init] Granted 'see the attendance list' to {n} role(s)")
        except Exception as e:
            db.session.rollback()
            print(f"[init] attendance.list backfill skipped: {e.__class__.__name__}")

        # The pass-printing rights are new. Anyone who could already generate
        # passes could already print them, so grant the three new keys to those
        # roles rather than silently taking printing away.
        try:
            granted = 0
            for r in Role.query.all():
                if r.key == 'super_admin':
                    continue
                pm = r.perm_map
                if not pm.get('pass.generate'):
                    continue
                changed = False
                for k in ('pass.print_card', 'pass.print_member', 'pass.print_office'):
                    if not pm.get(k):
                        pm[k] = True
                        changed = True
                if changed:
                    r.permissions = json.dumps(pm)
                    granted += 1
            if granted:
                db.session.commit()
                print(f"[init] Granted the pass-printing rights to {granted} role(s) "
                      f"that could already generate passes")
        except Exception as e:
            db.session.rollback()
            print(f"[init] print-rights backfill skipped: {e.__class__.__name__}")

        # Users created before the Team Member / Center type existed: give them
        # a type from their role's scope, and clear the leftover HO centre on
        # anyone who works across all centres - otherwise the TM/Center column
        # still reads "HO".
        try:
            fixed = 0
            for u in User.query.all():
                want = u.user_type or default_user_type(u.role)
                if u.user_type != want:
                    u.user_type = want
                    fixed += 1
                if want == 'team_member' and u.center_id and u.role != 'super_admin':
                    u.center_id = None
                    fixed += 1
            if fixed:
                db.session.commit()
                print(f"[init] Set the user type on {fixed} user record(s)")
        except Exception as e:
            db.session.rollback()
            print(f"[init] user type backfill skipped: {e.__class__.__name__}")

        # Attendance rows the old code could leave behind: a check-in whose
        # pass was later deleted, and a member checked in twice in one year.
        try:
            orphan = Attendance.query.filter(
                Attendance.pass_id.isnot(None),
                ~Attendance.pass_id.in_(db.session.query(Pass.id))).all()
            for a in orphan:
                db.session.delete(a)
            seen, dupes = set(), []
            for a in Attendance.query.order_by(Attendance.check_in_time).all():
                key = (a.member_id, a.pujan_year)
                if key in seen:
                    dupes.append(a)
                else:
                    seen.add(key)
            for a in dupes:
                db.session.delete(a)
            if orphan or dupes:
                db.session.commit()
                print(f"[init] Attendance cleanup: {len(orphan)} orphaned, "
                      f"{len(dupes)} duplicate row(s) removed")
        except Exception as e:
            db.session.rollback()
            print(f"[init] attendance cleanup skipped: {e.__class__.__name__}")

        try:
            made = seed_manuals()
            if made:
                print(f"[init] Loaded {made} user manual(s) from manuals/")
        except Exception as e:
            db.session.rollback()
            print(f"[init] manual loading skipped: {e.__class__.__name__}")

        # Accounts created while the default was Gujarati still carry 'gu'.
        # Repairing them here is what makes DEFAULT_LANGUAGE take effect on an
        # installation that already has users.
        try:
            normalize_user_language(
                force=os.environ.get('RESET_USER_LANGUAGE') == '1')
        except Exception as e:
            db.session.rollback()
            print(f"[init] language normalising skipped: {e.__class__.__name__}")

        try:
            made = seed_instructions()
            if made:
                print(f"[init] Seeded {made} instruction page(s)")
        except Exception as e:
            db.session.rollback()
            print(f"[init] instruction seeding skipped: {e.__class__.__name__}")

        try:
            made = seed_templates()
            if made:
                print(f"[init] Seeded {made} message template(s)")
        except Exception as e:
            db.session.rollback()
            print(f"[init] template seeding skipped: {e.__class__.__name__}")

        # The four built-in roles become editable rows.
        try:
            made = seed_roles()
            if made:
                print(f"[init] Seeded {made} built-in role(s)")
            # A build that adds a permission - attendance.darshan, say - has to
            # reach the roles seeded by an earlier one, or the right exists and
            # nobody has it.
            backfill_role_permissions()
        except Exception as e:
            db.session.rollback()
            print(f"[init] role seeding skipped: {e.__class__.__name__}: {e}")

        # An existing database may still have accounts on the seeded password.
        # Flagging them here means an upgrade closes that door too, not only a
        # fresh install.
        try:
            admin_pw = os.environ.get('ADMIN_PASSWORD', 'admin@123')
            sant_pw = os.environ.get('SANT_PASSWORD', 'sant@123')
            n = 0
            for u in User.query.all():
                if u.must_change_password or u.password_changed_at:
                    continue
                if u.check_password(admin_pw) or u.check_password(sant_pw):
                    u.must_change_password = True
                    n += 1
            if n:
                db.session.commit()
                print(f"[init] {n} account(s) still on a default password - "
                      f"they must change it at next sign-in")
        except Exception as e:
            db.session.rollback()
            print(f"[init] default-password check skipped: {e.__class__.__name__}")

        # Reading your own manual is not a privilege, so every role gets it -
        # which document they actually see is decided by the targeting. Done
        # here rather than in the earlier backfill, because that one runs
        # before the built-in roles exist on a fresh database.
        try:
            n = 0
            for r in Role.query.all():
                if r.key == 'super_admin':
                    continue
                pm = r.perm_map
                if not pm.get('manual.view'):
                    pm['manual.view'] = True
                    r.permissions = json.dumps(pm)
                    n += 1
            if n:
                db.session.commit()
                print(f"[init] Granted manual.view to {n} role(s)")
        except Exception as e:
            db.session.rollback()
            print(f"[init] manual.view grant skipped: {e.__class__.__name__}")

        # Members created before member_code existed need one, because it is
        # now required and unique.
        try:
            missing = Member.query.filter(
                db.or_(Member.member_code.is_(None), Member.member_code == '')).all()
            for m in missing:
                m.member_code = next_member_code(m.center_id)
                db.session.flush()
            if missing:
                db.session.commit()
                print(f"[init] Generated a Member ID for {len(missing)} member(s)")
        except Exception as e:
            db.session.rollback()
            print(f"[init] member_code backfill skipped: {e.__class__.__name__}")

        # Centres created before name_en existed show their Gujarati name in
        # the English UI. Backfill so the English column is never empty.
        try:
            fixed = Center.query.filter(
                db.or_(Center.name_en.is_(None), Center.name_en == '')).all()
            for c in fixed:
                c.name_en = c.name
            if fixed:
                db.session.commit()
                print(f"[init] Backfilled English name on {len(fixed)} center(s)")
        except Exception:
            db.session.rollback()

        if Center.query.first():
            print("[init] Database already initialised")
            return

        admin_pw = os.environ.get('ADMIN_PASSWORD', 'admin@123')
        sant_pw = os.environ.get('SANT_PASSWORD', 'sant@123')

        ho = Center(name='Head Office (HO)', name_en='Head Office (HO)',
                    city='Ahmedabad', phone='079-XXXXXXX')
        db.session.add(ho)
        db.session.flush()

        admin = User(username='admin', full_name='SMVS Admin', role='super_admin',
                     center_id=ho.id, mobile='9999999999', email='admin@smvs.org')
        admin.set_password(admin_pw)
        # The seeded passwords are written down in this repository, so they are
        # good for exactly one sign-in. Nothing opens until they are changed.
        admin.must_change_password = True
        db.session.add(admin)

        # BUGFIX: the original loop built every sant username from the LAST
        # value of name_en, so all five collided on a unique column and the
        # entire seeding transaction rolled back - including the admin user.
        # The username is now derived from the center being created.
        for name_gu, name_en, city in DEFAULT_CENTERS:
            c = Center(name=name_gu, name_en=name_en, city=city)
            db.session.add(c)
            db.session.flush()
            uname = (name_en or name_gu).lower().replace(' center', '').replace(' ', '_') + '_sant'
            sant = User(username=uname, full_name=f'{name_gu} Sant',
                        role='center_sant', center_id=c.id)
            sant.set_password(sant_pw)
            sant.must_change_password = True
            db.session.add(sant)

        db.session.commit()
        print("[init] Seeded: admin + %d centers with their sant logins" % len(DEFAULT_CENTERS))
        print("[init] CHANGE THE DEFAULT PASSWORDS BEFORE GOING LIVE.")


def start_scheduler():
    """Wakes once a minute and sends whatever is due.

    Every worker runs one of these, which is fine: a schedule is claimed with a
    conditional UPDATE before anything goes out, so only one of them wins. Set
    SCHEDULER=0 to leave it off and drive it from cron against
    POST /api/schedules/run-due instead.
    """
    if os.environ.get('SCHEDULER', '1') != '1':
        print('[init] Scheduler off (SCHEDULER=0)')
        return

    def loop():
        time.sleep(20)                    # let the app finish starting
        while True:
            try:
                with app.app_context(), scheduler_request_context():
                    ran = run_due_schedules()
                    for r in ran:
                        print(f"[schedule] {r['name']}: {r['result']}")
            except Exception as e:
                print(f'[schedule] skipped this pass: {e.__class__.__name__}: {e}')
            time.sleep(60)

    threading.Thread(target=loop, name='schedule-runner', daemon=True).start()


start_scheduler()


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 3000)),
            debug=os.environ.get('FLASK_DEBUG') == '1')