from telegram.ext import PicklePersistence
import psycopg2, psycopg2.extras
import sys

data = PicklePersistence(filename="lokisnbot.data", store_user_data=True, store_chat_data=False, on_flush=True)

pgsql = psycopg2.connect(dbname='lokisnbot', connection_factory=psycopg2.extras.LoggingConnection)
pgsql.initialize(sys.stderr)

for telegram_id, d in data.get_user_data().items():
    cur = pgsql.cursor()
    cur.execute("INSERT INTO users (telegram_id) VALUES (%s) RETURNING id", (telegram_id,))
    uid = cur.fetchone()[0]
    for sn in (d['sn'] if 'sn' in d else []):
        sn_row = {
                'pubkey': None,
                'active': False,
                'complete': False,
                'expires_soon': False, # True now, but was false before the PGSQL rewrite
                'last_contributions': None,
                'last_reward_block_height': None,
                'alias': None,
                'note': None,
                'notified_dereg': False,
                'notified_uptime_age': None,
                'rewards': False,
                'expiry_notified': None,
                'notified_age': None,
                }
        if 'lrbh' in sn:
            sn['last_reward_block_height'] = sn['lrbh']
        for k in sn_row.keys():
            if k in sn:
                sn_row[k] = sn[k]

        ins_cols, ins_vals = ['uid'], [uid]
        for k, v in sn_row.items():
            ins_cols.append(k)
            ins_vals.append(v)

        ins_cols = ", ".join(ins_cols)
        ins_vals = tuple(ins_vals)
        cur.execute("INSERT INTO service_nodes (" + ins_cols + ") VALUES %s",
                (ins_vals,))

    for w in (d['wallets'] if 'wallets' in d else set()):
        cur.execute("INSERT INTO wallet_prefixes (uid, wallet) VALUES (%s, %s)", (uid, w))

    pgsql.commit()

