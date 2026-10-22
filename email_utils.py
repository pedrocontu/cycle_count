import smtplib
from email.message import EmailMessage
from email.utils import make_msgid, formatdate

EMAIL_CONFIG = {
    'host': 'vistsmtp.visteon.com',
    'port': 25,
    'from_email': 'noreply@visteon.com',
    'use_tls': False,
}


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
    cc_list = _clean_addrs(cc)
    bcc_list = _clean_addrs(bcc)
    if not to_list and not cc_list and not bcc_list:
        raise ValueError('No recipients (to/cc/bcc all empty).')
    msg = EmailMessage()
    msg['From'] = from_email or EMAIL_CONFIG['from_email']
    msg['To'] = ', '.join(to_list)
    if cc_list:
        msg['Cc'] = ', '.join(cc_list)
    msg['Subject'] = subject
    msg['Date'] = formatdate(localtime=True)
    mid = make_msgid(domain='visteon.com')
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
