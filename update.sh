#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
target_dir="$source_dir"
target_given=false
manual=false
backup_dir=""
new_files=()
copied=false

managed_files=(bot.py sandbox.py attachments.py dialogues.py access.py evidence.py
    redaction.py credentials.py encrypt-api-key.py telegram_format.py llmbot.service
    llmbot-system.service requirements.txt HELP.md VERSION .env.example install.sh update.sh)

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

restore() {
    trap - ERR
    set +e
    if [[ "$copied" == true && -n "$backup_dir" && -d "$backup_dir" ]]; then
        local file
        for file in "${managed_files[@]}"; do
            if [[ -f "$backup_dir/$file" ]]; then
                cp -a -- "$backup_dir/$file" "$target_dir/$file" >/dev/null 2>&1
            fi
        done
        for file in "${new_files[@]}"; do
            rm -f -- "$target_dir/$file" >/dev/null 2>&1
        done
        if ! "$manual" && [[ -f "$target_dir/.env" ]]; then
            systemctl --user restart llmbot.service >/dev/null 2>&1
        fi
        rm -rf -- "$backup_dir"
        fail "Обновление прервано. Предыдущая версия восстановлена; проверьте статус сервиса."
    fi
    rm -rf -- "$backup_dir"
    fail "Обновление прервано. Проверьте статус сервиса."
}

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --target)
            [[ "$#" -ge 2 && -d "$2" ]] || fail "После --target укажите существующий каталог бота."
            target_dir="$(cd -- "$2" && pwd -P)"
            target_given=true
            shift 2
            ;;
        --no-service)
            manual=true
            shift
            ;;
        -h|--help)
            cat <<'EOF'
Использование: ./update.sh [--target КАТАЛОГ] [--no-service]
От root обновляется системная установка в /opt/llmbot.
Для пользовательской установки укажите каталог через --target.
--no-service подходит для обновления установки с ручным запуском.
При сбое обновления заменённые файлы восстанавливаются из резервной копии.
EOF
            exit 0
            ;;
        *) fail "Неизвестный параметр: $1. См. ./update.sh --help." ;;
    esac
done

if [[ "$EUID" -eq 0 ]] && ! "$manual"; then
    if "$target_given"; then
        fail "Системная установка обновляется в /opt/llmbot. Запустите ./update.sh без --target."
    fi
    [[ -f /etc/llmbot/.env && -f /opt/llmbot/bot.py ]] ||
        fail "Системная установка llmbot не найдена. Для первой установки используйте ./install.sh."
    # install.sh сам делает резервную копию /opt/llmbot и откатывается при сбое.
    exec bash "$source_dir/install.sh"
fi

[[ -f "$target_dir/.env" && -f "$target_dir/bot.py" ]] ||
    fail "Укажите каталог существующего бота через --target или используйте ./install.sh."

if [[ "$target_dir" != "$source_dir" ]]; then
    backup_dir="$(mktemp -d)"
    for file in "${managed_files[@]}"; do
        if [[ -f "$target_dir/$file" ]]; then
            cp -a -- "$target_dir/$file" "$backup_dir/$file"
        else
            new_files+=("$file")
        fi
    done
    trap 'restore' ERR
    copied=true
    install -m 0644 -- "$source_dir/"{bot.py,sandbox.py,attachments.py,dialogues.py,access.py,evidence.py,redaction.py,credentials.py,encrypt-api-key.py,telegram_format.py,llmbot.service,llmbot-system.service,requirements.txt,HELP.md,VERSION,.env.example} "$target_dir/"
    install -m 0755 -- "$source_dir/"{install.sh,update.sh} "$target_dir/"
    for config in prompt.txt telegram-format.txt; do
        if [[ ! -e "$target_dir/$config" ]]; then
            install -m 0644 -- "$source_dir/$config" "$target_dir/$config"
        fi
    done
fi

if "$manual"; then
    if ! bash "$target_dir/install.sh" --no-service; then
        restore
    fi
else
    if ! bash "$target_dir/install.sh"; then
        restore
    fi
fi
trap - ERR
rm -rf -- "$backup_dir"
