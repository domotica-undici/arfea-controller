"""Firme di errori e warning di un HABApp.log, per confrontare due esecuzioni.

Una firma per eccezione (tipo, messaggio, funzione del nostro codice dove nasce) o
per warning, senza orari, numeri e nomi di item: due log dello stesso codice su due
motori danno le stesse firme se il motore non cambia niente.

  python3 -I firme.py HABApp.log                      # firme con il numero di volte
  python3 -I firme.py base.log altro.log              # solo nella base / solo nell'altro
  python3 -I firme.py --solo-nuove base.log altro.log # solo quelle che la base non ha

Lo usa script/prova-habapp.sh.
"""
import re
import sys
from collections import Counter

LINE = re.compile(r'^\[[^\]]*\] \[\s*([^\]]+?)\]\s+(\w+) \| (.*)$')
OUR_FRAME = re.compile(r'\s*File "/habapp/config/([^"]+)", line \d+, in (\w+)')
EXCEPTION = re.compile(r'([A-Za-z_.]+(?:Error|Exception|Warning)): (.*)')


def firme(path: str) -> Counter:
    out: Counter = Counter()
    where = None
    with open(path, encoding='utf-8', errors='replace') as fh:
        for raw in fh:
            m = LINE.match(raw.rstrip('\n'))
            if not m:
                continue
            logger, level, msg = m.groups()
            if level not in ('ERROR', 'WARNING', 'CRITICAL'):
                continue

            # traceback: l'ultima riga del nostro codice, poi l'eccezione
            fm = OUR_FRAME.match(msg)
            if fm:
                where = f'{fm.group(1)}:{fm.group(2)}'
                continue
            em = EXCEPTION.match(msg)
            if em and level != 'WARNING':
                text = re.sub(r"'[^']*'", "'…'", em.group(2))[:120]
                out[f'EXC {em.group(1)}: {text} @ {where}'] += 1
                where = None
                continue

            if level == 'WARNING':
                text = re.sub(r'(/rest/items/)\S+ .*', r'\1<item> <valore>', msg)
                text = re.sub(r'Added another rule of type \w+', 'Added another rule of type <tipo>', text)
                text = re.sub(r'\b(T[A-Z][A-Za-z0-9_]+|hbt[A-Za-z0-9_]+|[a-z]+[A-Z][A-Za-z0-9_]*)\b', '<nome>', text)
                text = re.sub(r'\d+', 'N', text)
                out[f'WARN [{logger}] {text[:140]}'] += 1
            elif not msg.strip(' -') or msg.lstrip().startswith(('^', '-->', 'File ', 'ret = ')):
                continue    # cornici e righe di contesto dei traceback
            elif logger.startswith('HABApp.connection') or level == 'CRITICAL':
                text = re.sub(r'^Input: \{"type":"(\w+)".*', r'Input: \1 <evento>', msg.strip())
                text = re.sub(r'\d+', 'N', text)
                out[f'ERR [{logger}] {text[:120]}'] += 1
    return out


def main(argv: list[str]) -> int:
    only_new = '--solo-nuove' in argv
    files = [a for a in argv if a != '--solo-nuove']
    if len(files) == 1:
        for k, v in sorted(firme(files[0]).items()):
            print(f'{v:6d}  {k}')
        return 0
    if len(files) != 2:
        print(__doc__)
        return 2

    a, b = firme(files[0]), firme(files[1])
    if not only_new:
        print(f'--- solo in {files[0]}')
        for k in sorted(set(a) - set(b)):
            print(f'{a[k]:6d}  {k}')
        print(f'+++ solo in {files[1]}')
    new = sorted(set(b) - set(a))
    for k in new:
        print(f'{b[k]:6d}  {k}')
    if only_new and not new:
        print('  (nessuna)')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
