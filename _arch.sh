set -e
TAR=$(cygpath -u "$TEMP/arch.tar")
rm -rf /tmp/archtest; mkdir -p /tmp/archtest
tar -xf "$TAR" -C /tmp/archtest
echo "=== как файлы придут на Linux ==="
for f in /tmp/archtest/deploy/run_bot.sh /tmp/archtest/deploy/arb-bot.service; do
  printf '%-42s ' "$(basename $f)"
  if head -c 4000 "$f" | od -c | grep -q '\\r'; then
    echo "CRLF - СЛОМАНО"
  else
    echo "LF - OK"
  fi
done
echo "--- shebang первой строки ---"
head -1 /tmp/archtest/deploy/run_bot.sh | od -c | head -2
echo "--- синтаксис ---"
bash -n /tmp/archtest/deploy/run_bot.sh && echo "синтаксис в порядке"