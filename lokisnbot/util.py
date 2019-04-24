
import lokisnbot

def friendly_time(seconds):
    val = ''
    if seconds >= 86400:
        days = seconds // 86400
        seconds %= 86400
        if round(seconds / 3600, 1) == 24.0:
            # If hours is going to round up to 24.0 then just round up the days instead because
            # '3 days 24.0 hours' looks stupid.
            days += 1
            seconds = 0
        val += '{} day{} '.format(days, '' if days == 1 else 's')
        if seconds == 0:
            return val
    if seconds >= 3600:
        val += '{:.1f} hours'.format(seconds / 3600)
    elif seconds >= 60:
        val += '{:.0f} minutes'.format(seconds / 60)
    else:
        val += '{} seconds'.format(seconds)
    return val


def ago(seconds):
    return friendly_time(seconds) + ' ago'


def escape_markdown(text):
    return text.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")


def explorer(testnet=False):
    return (lokisnbot.config.TESTNET_EXPLORER or 'lokitestnet.com') if testnet else (lokisnbot.config.EXPLORER or 'lokiblocks.com')
