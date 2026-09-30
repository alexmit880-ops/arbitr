echo "=== ExecStart / WorkingDirectory в юните ==="
grep -E '^(ExecStart|WorkingDirectory|ReadWritePaths|User)=' /tmp/archtest/deploy/arb-bot.service
echo
echo "=== %h раскроется в домашний каталог пользователя ==="
echo "root -> /root/arbitr  (совпадает с ExecStart в статусе на риге)"
echo
echo "=== важно: в юните больше нет жёсткого /home/user ==="
if grep -q '/home/user' /tmp/archtest/deploy/arb-bot.service; then
  echo "ОСТАЛСЯ жёсткий путь:"
  grep -n '/home/user' /tmp/archtest/deploy/arb-bot.service
else
  echo "нет - используется %h, это правильно"
fi