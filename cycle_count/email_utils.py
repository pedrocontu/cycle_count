import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

EMAIL_CONFIG = {
    'host': None,
    'port': 25,
    'from_email': None,
    'use_tls': False,
    'domain': None,
}


def configure(host, port, use_tls, from_email, domain):
    EMAIL_CONFIG.update(host=host, port=port, use_tls=use_tls,
                        from_email=from_email, domain=domain)


def _clean_addrs(addrs):
    out, seen = [], set()

    def push(v):
        v = (v or '').strip()
        if not v:
            return
        k = v.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(v)

    if isinstance(addrs, str):
        addrs = [addrs]
    for a in (addrs or []):
        if a is None:
            continue
        if isinstance(a, (list, tuple, set)):
            for sub in a:
                for piece in str(sub or '').split(','):
                    push(piece)
        else:
            for piece in str(a).split(','):
                push(piece)
    return out


def send_email(to, subject, text_body, html_body=None,
               cc=None, bcc=None,
               in_reply_to=None, references=None,
               from_email=None):
    to_list = _clean_addrs(to)
    cc_list = [a for a in _clean_addrs(cc) if a.lower() not in {t.lower() for t in to_list}]
    bcc_list = _clean_addrs(bcc)
    if not to_list and not cc_list and not bcc_list:
        raise ValueError('No recipients (to/cc/bcc all empty).')
    if not EMAIL_CONFIG['host']:
        raise RuntimeError('SMTP host is not configured.')
    msg = EmailMessage()
    msg['From'] = from_email or EMAIL_CONFIG['from_email']
    if to_list:
        msg['To'] = ', '.join(to_list)
    if cc_list:
        msg['Cc'] = ', '.join(cc_list)
    msg['Subject'] = subject
    msg['Date'] = formatdate(localtime=True)
    mid = make_msgid(domain=EMAIL_CONFIG['domain'])
    msg['Message-ID'] = mid
    if in_reply_to:
        msg['In-Reply-To'] = in_reply_to
        msg['References'] = (references or in_reply_to).strip()
    msg.set_content(text_body or '')
    if html_body:
        msg.add_alternative(html_body, subtype='html')
    recipients = to_list + cc_list + bcc_list
    with smtplib.SMTP(EMAIL_CONFIG['host'], EMAIL_CONFIG['port'], timeout=20) as smtp:
        if EMAIL_CONFIG.get('use_tls'):
            smtp.starttls()
        smtp.send_message(msg, to_addrs=recipients)
    return mid
