# Установка в Linux

Прошивка дисплея остаётся прежней. Linux-программа отправляет тот же пакет `PCS`
через USB раз в секунду. LibreHardwareMonitor в Linux не нужен.

## Установка одним файлом

Скачайте `cyd-pc-stats-1.2.0-linux.run` и файл `.sha256` из
[релиза Linux](https://github.com/4uva4ek/esphome-cyd-pc-monitor/releases/tag/linux-v1.2.0).
Установщик содержит исходники программы; Git не требуется. Зависимости Python
загружаются из PyPI, поэтому при установке нужен интернет.

Нужны Python 3.9+, pip/venv, Bash, tar, awk. Для автозапуска нужен systemd.
Установите зависимости своей системы:

```bash
# CachyOS / Arch
sudo pacman -S --needed python python-pip
# Ubuntu / Debian
sudo apt install python3 python3-venv python3-pip
# Fedora
sudo dnf install python3 python3-pip
```

В каталоге со скачанными файлами выполните **от обычного пользователя, без sudo**:

```bash
sha256sum -c cyd-pc-stats-1.2.0-linux.run.sha256
bash cyd-pc-stats-1.2.0-linux.run
```

Программа установится в `~/.local/share/cyd-pc-stats`, зависимости — в отдельное
Python-окружение. Сервис `cyd-pc-stats.service` будет запускаться при входе в систему.
Переменные `XDG_DATA_HOME` и `XDG_CONFIG_HOME` учитываются, если заданы.
Установщик не меняет системные группы, не ставит драйверы и не прошивает дисплей.
Повторный запуск обновляет программу; нужные параметры следует передать снова.

Без systemd или без автозапуска:

```bash
bash cyd-pc-stats-1.2.0-linux.run --no-autostart
~/.local/share/cyd-pc-stats/run.sh
```

Этот режим также останавливает и отключает прежний сервис, если доступен
пользовательский менеджер systemd. `run.sh` можно добавить в автозагрузку рабочего стола.

Установка из исходников:

```bash
git clone https://github.com/4uva4ek/esphome-cyd-pc-monitor.git
cd esphome-cyd-pc-monitor
./install.sh
```

## USB-порт

Дисплей должен быть прошит одной из
[прошивок исходного проекта](https://github.com/nyburglar/esphome-cyd-pc-monitor/releases/latest).
Инструкция по выбору контроллера — в README. Порт определяется автоматически,
если подключён один адаптер CH340/CH9102/CP210x. При нескольких адаптерах
укажите нужный явно. Предпочтителен постоянный путь `/dev/serial/by-id/…`:

```bash
ls -l /dev/serial/by-id/ /dev/ttyUSB* /dev/ttyACM*
bash cyd-pc-stats-1.2.0-linux.run --port /dev/ttyUSB0
```

При `Permission denied` посмотрите группу устройства в выводе `ls -l`.
Для CachyOS/Arch это обычно `uucp`:

```bash
sudo usermod -aG uucp "$USER"
```

Если устройство принадлежит группе `dialout`, используйте её вместо `uucp`.
После этого полностью выйдите из всех сеансов и войдите снова либо перезагрузитесь.
Просто открыть новый терминал недостаточно для обновления групп сервиса systemd.
Запуск программы от root не требуется.

Перед прошивкой освободите порт:

```bash
systemctl --user stop cyd-pc-stats
# После прошивки:
systemctl --user start cyd-pc-stats
```

## Проверка и настройка

```bash
app="${XDG_DATA_HOME:-$HOME/.local/share}/cyd-pc-stats"
"$app/venv/bin/python" "$app/pc_stats_feeder.py" --list-sensors
"$app/venv/bin/python" "$app/pc_stats_feeder.py" --dry-run --once
systemctl --user status cyd-pc-stats
journalctl --user -u cyd-pc-stats -f
```

`--dry-run --once` считывает данные без обращения к USB и завершается после одного
интервала измерения. Для обновления раз в две секунды передайте установщику
`--interval 2`. Для выбора видеокарты доступны `--nvidia-gpu 0` (также UUID/PCI ID)
и `--drm-card card1` (AMD). Эти параметры нельзя использовать одновременно.

Вентилятор CPU выбирается автоматически только при наличии CPU в названии.
Для другого обозначения найдите его через `--list-sensors` и передайте
`--cpu-fan 'nct6799:fan2'`. Для AMD GPU доступен `--gpu-fan 'amdgpu:fan1'`.
Параметр `--verbose` добавляет отправленные пакеты в журнал.

## Какие датчики поддержаны

- AMD/Intel CPU: температура из hwmon (`k10temp`, `coretemp` и другие), загрузка и RAM из psutil.
- Мощность CPU: доступные счётчики энергии пакета RAPL/powercap либо датчик мощности пакета hwmon. Если ядро разрешает читать счётчик только root, поле остаётся пустым.
- NVIDIA: температура, загрузка, мощность и VRAM через `nvidia-smi` из установленного драйвера.
- AMD GPU: температура, загрузка, мощность, VRAM и обороты вентилятора через DRM/hwmon. У APU мощность может относиться ко всему SoC.
- Вентиляторы CPU: доступные датчики RPM из hwmon. Неподписанные корпусные вентиляторы не выдаются за CPU.
- NVIDIA RPM недоступны через используемый интерфейс: процент скорости нельзя отображать как обороты в минуту.
- Для Intel GPU отдельного источника метрик пока нет.

Недоступные показатели отображаются как `--`. На CachyOS с Ryzen 7 9800X3D и
RTX 5090 проверено чтение температуры и загрузки CPU, памяти, температуры,
загрузки, мощности и VRAM видеокарты. Счётчик мощности CPU в этой системе закрыт
правами доступа; датчики оборотов CPU не обнаружены. Передача проверена через
псевдотерминал; изображение на физическом CYD не проверялось.

## Удаление

```bash
bash "${XDG_DATA_HOME:-$HOME/.local/share}/cyd-pc-stats/uninstall.sh"
```

Удаляются программа и её сервис. Прошивка, системные пакеты, драйверы и группы
пользователя сохраняются. Подробности реализации и сборки — в [английской инструкции](linux.md).
