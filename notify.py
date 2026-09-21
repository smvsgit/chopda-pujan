"""Sending the pujan instructions by email and WhatsApp.

What is real and what is not
----------------------------
* **Email** genuinely sends, over SMTP, when the SMTP_* environment variables
  are set (see .env.example). Without them nothing is sent and every row is
  logged as `not_configured` rather than being quietly marked "sent" — the old
  `send_notification()` used to claim success while doing nothing at all.
* **WhatsApp** does not send by itself. There is no free unattended WhatsApp
  API; the Business API needs an approved provider and pre-approved message
  templates. So this builds ready-to-send `wa.me` links, one per member, that
  a volunteer clicks through. If you later sign up with a provider, implement
  `send_whatsapp_via_provider()` and the rest of the flow is unchanged.

Messages are rendered from templates held in EventConfig, so the date, venue
and wording change from the admin screen rather than from the code.
"""

import json
import os
import urllib.parse
import urllib.request
import re
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

SMTP_HOST = os.environ.get('SMTP_HOST', '')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER', '')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
SMTP_FROM = os.environ.get('SMTP_FROM', SMTP_USER or 'no-reply@smvs.org')
SMTP_FROM_NAME = os.environ.get('SMTP_FROM_NAME', 'SMVS Chopda-Pujan')
SMTP_TLS = os.environ.get('SMTP_TLS', '1') != '0'

COUNTRY_CODE = os.environ.get('WHATSAPP_COUNTRY_CODE', '91')

_PLACEHOLDER = re.compile(r'\{([a-z_]+)\}')


def email_configured():
    """A host is not enough - there has to be an address to send as."""
    return bool(SMTP_HOST) and bool(SMTP_FROM or SMTP_USER)


def render(template, context):
    """Fill {placeholders}. An unknown one is left visible rather than
    exploding, so a typo in the admin screen is obvious in the preview."""
    def sub(m):
        key = m.group(1)
        val = context.get(key)
        return '' if val is None else str(val)
    return _PLACEHOLDER.sub(sub, template or '')


def placeholders_in(template):
    return sorted(set(_PLACEHOLDER.findall(template or '')))


def normalise_phone(raw):
    """9925242806 -> 919925242806. Returns None if it cannot be used."""
    digits = re.sub(r'\D', '', str(raw or ''))
    if not digits:
        return None
    if digits.startswith('00'):
        digits = digits[2:]
    if len(digits) == 10:
        digits = COUNTRY_CODE + digits
    if len(digits) < 11 or len(digits) > 15:
        return None
    return digits


def whatsapp_link(phone, message):
    num = normalise_phone(phone)
    if not num:
        return None
    return f'https://wa.me/{num}?text={quote(message)}'


def _connect():
    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30,
                                  context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        if SMTP_TLS:
            server.starttls(context=ssl.create_default_context())
    if SMTP_USER:
        server.login(SMTP_USER, SMTP_PASSWORD)
    return server


def from_address():
    """The address to send as.

    SMTP_FROM is meant to be an email address, but a display name gets typed
    in there easily enough - and `From: Name <MK>` is rejected by every mail
    server, with a message that says nothing about where the fault is. So an
    entry with no @ in it is ignored and the login address used instead, which
    is what almost every provider requires anyway.
    """
    if SMTP_FROM and '@' in SMTP_FROM:
        return SMTP_FROM
    return SMTP_USER or ''


# The tags a template may use. Whoever writes a template is an admin, not
# the public, so this is about what mail clients survive rather than about
# defending against them: Gmail and Outlook both drop stylesheets, <script>,
# <style> and most attributes, so anything not on this list would be
# stripped by the reader anyway and is better stripped here where the plain
# text version can be kept honest.
ALLOWED_TAGS = {'b', 'strong', 'i', 'em', 'u', 'br', 'p', 'ul', 'ol', 'li',
                'a', 'span', 'div', 'h3', 'h4', 'small', 'hr'}
_TAG_RE = re.compile(r'<\s*(/?)\s*([a-zA-Z0-9]+)((?:[^<>"\']|"[^"]*"|\'[^\']*\')*)>')
_HREF_RE = re.compile(r'href\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', re.I)
_URL_RE = re.compile(r'https?://[^\s<>"\']+')
# Tags that lay a message out. Their presence means the author is handling the
# line breaks; without them, the newlines in the box are the layout.
_BLOCK_RE = re.compile(r'<\s*/?\s*(br|p|div|ul|ol|li|h3|h4|hr)\b', re.I)
# script and style are removed with their contents. Every other unknown tag
# loses the tag and keeps the words, which is right for a stray <table> and
# wrong for these two - "bad()" is not a sentence anybody meant to send.
_DROP_WHOLE_RE = re.compile(
    r'<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>|<\s*(script|style)\b[^>]*/?>',
    re.I | re.S)


_ANCHOR_RE = re.compile(r'<\s*a\b([^>]*)>(.*?)<\s*/\s*a\s*>', re.I | re.S)


def _drop_bad_anchors(text):
    """Removes links we will not render, label and all.

    Dropping only the tag would leave "Click here" sitting in the message with
    nothing behind it - misleading on its own, and it runs into whatever
    follows. A link whose href we refuse is not one whose wording is worth
    keeping.
    """
    def check(m):
        href = ''
        hm = _HREF_RE.search(m.group(1) or '')
        if hm:
            href = (hm.group(2) or hm.group(3) or hm.group(4) or '').strip()
        if re.match(r'^(https?:|mailto:)', href, re.I):
            return m.group(0)
        return ''
    return _ANCHOR_RE.sub(check, text or '')


def looks_like_html(text):
    """True when a template was written with markup in it."""
    return bool(_TAG_RE.search(text or ''))


def sanitise_html(text):
    """The template's own markup, with anything unexpected taken out.

    Tags off the list are dropped but their text is kept, so a stray <table>
    loses the box and not the sentence. Attributes go except href on a link,
    and a href has to be http, https or mailto - which stops a template
    carrying a javascript: link into somebody's mail client.

    Rejecting a link has to drop its </a> as well, or the closing tag is left
    behind to wrap whatever follows it.
    """
    text = _drop_bad_anchors(_DROP_WHOLE_RE.sub('', text or ''))
    pending_a = [0]        # a bare </a> still to be swallowed

    def keep(m):
        closing, tag, attrs = m.group(1), m.group(2).lower(), m.group(3) or ''
        if tag not in ALLOWED_TAGS:
            return ''
        if closing:
            if tag == 'a' and pending_a[0] > 0:
                pending_a[0] -= 1
                return ''
            return f'</{tag}>'
        if tag == 'a':
            href = ''
            hm = _HREF_RE.search(attrs)
            if hm:
                href = (hm.group(2) or hm.group(3) or hm.group(4) or '').strip()
            if not re.match(r'^(https?:|mailto:)', href, re.I):
                pending_a[0] += 1
                return ''
            safe = href.replace('"', '%22')
            return f'<a href="{safe}" target="_blank" rel="noopener">'
        return f'<{tag}>'

    return _TAG_RE.sub(keep, text)


def autolink(html):
    """Bare URLs turned into links, leaving existing ones alone.

    Split on whole anchors and on tags first, so a URL already inside an
    href is never wrapped a second time.
    """
    parts = re.split(r'(<a\b[^>]*>.*?</a>|<[^>]+>)', html or '',
                     flags=re.I | re.S)
    out = []
    for i, part in enumerate(parts):
        if i % 2:                       # a tag, or a whole anchor
            out.append(part)
        else:
            out.append(_URL_RE.sub(
                lambda m: f'<a href="{m.group(0)}" target="_blank" '
                          f'rel="noopener">{m.group(0)}</a>', part))
    return ''.join(out)


def strip_tags(text):
    """The markup taken out, for the plain-text part and for SMS.

    Block edges become line breaks first, so a list does not arrive as one
    run of words.
    """
    out = _drop_bad_anchors(_DROP_WHOLE_RE.sub('', text or ''))
    # A link becomes "words: address". Keeping only the words would leave a
    # text-only reader with "Click here" and nothing to click.
    def _flatten(m):
        href = ''
        hm = _HREF_RE.search(m.group(1) or '')
        if hm:
            href = (hm.group(2) or hm.group(3) or hm.group(4) or '').strip()
        label = re.sub(r'<[^>]+>', '', m.group(2) or '').strip()
        if not href:
            return label
        if not label or label == href:
            return href
        return f'{label}: {href}'
    out = _ANCHOR_RE.sub(_flatten, out)
    out = re.sub(r'<\s*br\s*/?\s*>', '\n', out, flags=re.I)
    out = re.sub(r'<\s*(p|div|ul|ol|h3|h4|hr)[^>]*>', '\n', out, flags=re.I)
    out = re.sub(r'<\s*/\s*(p|div|li|ul|ol|h3|h4)\s*>', '\n', out, flags=re.I)
    out = re.sub(r'<\s*li[^>]*>', '\u2022 ', out, flags=re.I)
    out = re.sub(r'<[^>]+>', '', out)
    out = (out.replace('&nbsp;', ' ').replace('&amp;', '&')
              .replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"'))
    out = re.sub(r'[ \t]+\n', '\n', out)
    return re.sub(r'\n{3,}', '\n\n', out).strip()


def to_whatsapp(text):
    """Markup turned into WhatsApp's own emphasis rather than thrown away."""
    out = re.sub(r'<\s*(b|strong)\s*>(.*?)<\s*/\s*\1\s*>', r'*\2*',
                 text or '', flags=re.I | re.S)
    out = re.sub(r'<\s*(i|em)\s*>(.*?)<\s*/\s*\1\s*>', r'_\2_',
                 out, flags=re.I | re.S)
    return strip_tags(out)


def _html_body(text, cid=None):
    """The message as HTML, with the QR beneath it when there is one.

    A template written with markup is used as it stands, sanitised. One
    written as plain text is escaped and its line breaks turned into <br>, so
    the years of existing templates keep working unchanged.

    Bare URLs are turned into links, because a member reading on a phone
    should not have to select and copy one.

    Deliberately plain markup and inline styles only: mail clients drop
    stylesheets, and this has to read the same in Gmail on a phone as in
    Outlook.
    """
    # Whether the newlines still need turning into <br> depends on what kind
    # of markup is in the message, not merely on whether there is any.
    #
    # A placeholder like {qr} or {my_link} expands into an <a> tag, so a
    # plain-text template that used neither ends up containing markup the
    # moment it is rendered. Treating that as "the author laid this out
    # themselves" threw the blank lines away and delivered the whole message
    # as one paragraph.
    #
    # So: block-level tags mean the author is doing the layout and the
    # newlines are theirs to keep. Inline tags only - <b>, <i>, <a> - and the
    # line breaks still have to be honoured.
    has_block = bool(_BLOCK_RE.search(text or ''))
    if looks_like_html(text):
        para = sanitise_html(text)
        if not has_block:
            para = para.replace('\r\n', '\n').replace('\n', '<br>')
    else:
        safe = (text.replace('&', '&amp;').replace('<', '&lt;')
                    .replace('>', '&gt;'))
        para = safe.replace('\r\n', '\n').replace('\n', '<br>')
    para = autolink(para)
    qr = ''
    if cid:
        qr = ('<div style="margin-top:22px;padding-top:18px;'
              'border-top:1px solid #EADFC8;text-align:center">'
              f'<img src="cid:{cid}" alt="Pass QR" width="200" height="200" '
              'style="display:block;margin:0 auto;border:1px solid #EADFC8;'
              'border-radius:8px"></div>')
    return (
        '<html><body style="margin:0;padding:0;background:#ffffff">'
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        'line-height:1.6;color:#3B2314;max-width:620px;padding:18px">'
        f'<div>{para}</div>{qr}</div></body></html>')


def _as_attachment(att):
    """(name, mime, bytes) from whatever the caller passed.

    Raw image bytes are accepted as well as the triple. Unpacking a caller's
    bytes into three names raised "too many values to unpack" from deep inside
    the mail code, where nothing pointed at the poster as the cause.
    """
    if isinstance(att, (bytes, bytearray)):
        return ('poster.jpg', 'image/jpeg', bytes(att))
    name, mime, data = att
    return (name or 'poster.jpg', mime or 'image/jpeg', data)


def send_emails(items, attachment=None):
    """items: [{'to', 'subject', 'body', 'ref'}]. attachment: (name, mime, bytes).

    'email' is accepted in place of 'to' and 'message' in place of 'body',
    because the SMS and WhatsApp senders beside this one use those names and
    callers reasonably assumed all three matched. Reading either way round is
    better than one screen quietly failing on a key name.

    One connection for the whole batch. Returns a result per item so a single
    bad address does not sink the run.
    """
    results = []
    if not email_configured():
        return [{'ref': i.get('ref'), 'ok': False, 'status': 'not_configured',
                 'error': 'SMTP is not configured on the server'} for i in items]
    sender = from_address()
    if not sender:
        return [{'ref': i.get('ref'), 'ok': False, 'status': 'not_configured',
                 'error': 'No usable From address. Set SMTP_FROM to an email '
                          'address, not a name.'} for i in items]
    server = None
    try:
        server = _connect()
        for it in items:
            try:
                addr = (it.get('to') or it.get('email') or '').strip()
                if not addr:
                    results.append({'ref': it.get('ref'), 'ok': False,
                                    'status': 'failed',
                                    'error': 'No email address for this member'})
                    continue
                msg = EmailMessage()
                msg['Subject'] = it.get('subject') or 'SMVS Chopda-Pujan'
                msg['From'] = f'{SMTP_FROM_NAME} <{sender}>'
                msg['To'] = addr
                raw = it.get('body') or it.get('message') or ''
                # The plain part never carries markup, whether or not the
                # template used any.
                text = strip_tags(raw) if looks_like_html(raw) else raw
                msg.set_content(text)
                # An item carrying its QR gets an HTML part with the image
                # attached to the message itself. A URL in a plain-text mail
                # is only ever a URL, and a remote <img> is blocked by default
                # in most mail clients - an attached image referred to by cid
                # is the one form that actually appears in the message.
                qr = it.get('qr_png')
                if qr or looks_like_html(raw):
                    # Named after the pass, so a mail client that keeps
                    # parts by content-id cannot confuse two messages.
                    cid = ('qr-' + re.sub(r'\W+', '',
                                          str(it.get('qr_name')
                                              or it.get('ref') or 'pass'))
                           ) if qr else None
                    msg.add_alternative(_html_body(raw, cid), subtype='html')
                    if qr:
                        msg.get_payload()[-1].add_related(
                            qr, maintype='image', subtype='png',
                            cid=f'<{cid}>',
                            filename=(it.get('qr_name') or 'pass-qr.png'),
                            disposition='inline')
                if attachment:
                    name, mime, data = _as_attachment(attachment)
                    maintype, _, subtype = mime.partition('/')
                    msg.add_attachment(data, maintype=maintype or 'image',
                                       subtype=subtype or 'jpeg', filename=name)
                server.send_message(msg)
                results.append({'ref': it.get('ref'), 'ok': True, 'status': 'sent'})
            except Exception as e:
                results.append({'ref': it.get('ref'), 'ok': False, 'status': 'failed',
                                'error': f'{e.__class__.__name__}: {e}'})
    except Exception as e:
        done = {r['ref'] for r in results}
        for it in items:
            if it.get('ref') not in done:
                results.append({'ref': it.get('ref'), 'ok': False, 'status': 'failed',
                                'error': f'SMTP connection failed: {e}'})
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass
    return results


# ---------------------------------------------------------------------------
# SMS through TextGuru
#
# Their API takes the login, password and sender id as query parameters and
# answers with a plain-text body rather than a documented JSON shape. So
# anything that is not clearly a success counts as a failure, and the body is
# kept for the log - a rejected DLT template or an empty balance then shows up
# as the real reason instead of a generic error.
# ---------------------------------------------------------------------------
TEXTGURU_URL = os.environ.get('TEXTGURU_URL', 'https://www.textguru.in/api/sendsms.php')


def sms_configured():
    """True when TextGuru credentials are present."""
    return bool(os.environ.get('TEXTGURU_LOGINID')
                and os.environ.get('TEXTGURU_PASSWORD')
                and os.environ.get('TEXTGURU_SENDERID'))


def whatsapp_configured():
    """True when a WhatsApp Business provider is configured."""
    return bool(os.environ.get('WHATSAPP_API_KEY')
                and os.environ.get('WHATSAPP_BASE_URL')
                and os.environ.get('WHATSAPP_FROM_NUMBER'))


def _http_detail(e):
    """An error line that says what the gateway objected to.

    urllib raises HTTPError for a 4xx, and the useful part is in the response
    body - "IP not whitelisted", "invalid template id", "missing field
    messaging_product". Left unread, all of that is thrown away and the log
    shows only "HTTP Error 403: Forbidden", which is true and useless.

    HTTPError is itself a readable file, so the body is right there.
    """
    code = getattr(e, 'code', None)
    body = ''
    try:
        raw = e.read()
        if raw:
            body = raw.decode('utf-8', 'replace').strip()
    except Exception:
        pass
    body = re.sub(r'\s+', ' ', body)[:400]
    if code and body:
        return f'{code}: {body}'
    if code:
        return f'{code}: {getattr(e, "reason", "") or e}'
    return f'{e.__class__.__name__}: {e}'


def _ten_digit(phone):
    """An Indian mobile as ten digits, or None.

    Numbers are stored however they were typed - with +91, a leading zero,
    spaces or dashes - and both gateways want ten digits.
    """
    digits = re.sub(r'\D', '', str(phone or ''))
    if len(digits) > 10:
        digits = digits[-10:]
    return digits if len(digits) == 10 and digits[0] in '6789' else None


def send_sms_via_provider(items):
    """Send each message through TextGuru.

    `items` is a list of {'phone': ..., 'message': ...}.
    Returns a list of {'ok': bool, 'error': str} in the same order.
    """
    if not sms_configured():
        return [{'ok': False, 'error': 'No SMS gateway configured'} for _ in items]

    login = os.environ['TEXTGURU_LOGINID']
    pwd = os.environ['TEXTGURU_PASSWORD']
    sender = os.environ['TEXTGURU_SENDERID']
    entity_id = os.environ.get('TEXTGURU_ENTITYID', '')
    template_id = os.environ.get('TEXTGURU_TEMPLATEID', '')

    out = []
    for it in items:
        num = _ten_digit(it.get('phone'))
        if not num:
            out.append({'ok': False, 'error': f"Not a mobile number: {it.get('phone')!r}"})
            continue
        # No mail client here: markup would arrive as literal < and >.
        params = {'username': login, 'password': pwd, 'sender': sender,
                  'mobile': num, 'message': strip_tags(it.get('message', ''))}
        # Unicode has to be declared or Gujarati arrives as question marks.
        if any(ord(ch) > 127 for ch in params['message']):
            params['type'] = '2'
            params['unicode'] = '1'
        if entity_id:
            params['entityid'] = entity_id
        if template_id:
            params['templateid'] = template_id
        try:
            url = TEXTGURU_URL + '?' + urllib.parse.urlencode(params)
            with urllib.request.urlopen(url, timeout=25) as r:
                body = (r.read() or b'').decode('utf-8', 'replace').strip()
                status = r.status
            low = body.lower()
            failed = any(w in low for w in ('error', 'invalid', 'fail', 'insufficient',
                                            'unauthor', 'blocked', 'reject'))
            if status == 200 and body and not failed:
                out.append({'ok': True, 'error': None, 'response': body[:200]})
            else:
                out.append({'ok': False, 'error': f'TextGuru said: {body[:180] or status}'})
        except Exception as e:
            # The gateway's own words, not just the status line.
            out.append({'ok': False, 'error': _http_detail(e)})
    return out


def send_whatsapp_via_provider(items):
    """Send each message through the configured WhatsApp provider.

    `items` is a list of {'phone': ..., 'message': ..., optional 'media_url'}.
    Returns a list of {'ok': bool, 'error': str} in the same order.

    Written against the shape most Indian resellers use: a base URL, an API key
    and a sender number, posted as JSON. If your provider expects something
    else, the payload below is the only part to change.

    Without credentials this returns a clear "not configured" for each and the
    caller falls back to wa.me links, so the work is never lost.
    """
    if not whatsapp_configured():
        return [{'ok': False, 'error': 'No WhatsApp provider configured'} for _ in items]

    base = os.environ['WHATSAPP_BASE_URL'].rstrip('/')
    key = os.environ['WHATSAPP_API_KEY']
    frm = os.environ['WHATSAPP_FROM_NUMBER']
    path = os.environ.get('WHATSAPP_SEND_PATH', '/messages')
    cc = os.environ.get('WHATSAPP_COUNTRY_CODE', '91')
    # Providers hand out base URLs with the send path already on the end. Left
    # alone, that becomes .../messages/messages and every send comes back 404
    # - which reads like a broken account rather than a doubled path.
    if path and base.endswith(path.rstrip('/')):
        path = ''
    endpoint = base + path

    out = []
    for it in items:
        num = _ten_digit(it.get('phone'))
        if not num:
            out.append({'ok': False, 'error': f"Not a mobile number: {it.get('phone')!r}"})
            continue
        # The shape the Meta Cloud API and the resellers built on it expect.
        # messaging_product is required and its absence is a plain 400 Bad
        # Request; the sender is identified by the URL, so 'from' in the body
        # is at best ignored and at worst another reason to reject it.
        payload = {'messaging_product': 'whatsapp',
                   'recipient_type': 'individual',
                   'to': cc + num,
                   'type': 'image' if it.get('media_url') else 'text'}
        if frm and 'cloud' not in base:
            # Some resellers do read a sender field. Harmless where it is not.
            payload['from'] = frm
        if it.get('media_url'):
            payload['image'] = {'link': it['media_url'], 'caption': it.get('message', '')}
        else:
            # WhatsApp has its own emphasis, so *bold* survives rather
            # than being dropped with the tag.
            payload['text'] = {'body': to_whatsapp(it.get('message', ''))}
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Bearer ' + key,
                         'apikey': key},        # providers differ on which they read
                method='POST')
            with urllib.request.urlopen(req, timeout=25) as r:
                body = (r.read() or b'').decode('utf-8', 'replace').strip()
            if 200 <= r.status < 300:
                out.append({'ok': True, 'error': None, 'response': body[:200]})
            else:
                out.append({'ok': False, 'error': f'{r.status}: {body[:180]}'})
        except Exception as e:
            # The gateway's own words, not just the status line.
            out.append({'ok': False, 'error': _http_detail(e)})
    return out