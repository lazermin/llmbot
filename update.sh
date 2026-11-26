#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
target_dir="$source_dir"
target_given=false
manual=false

fail() {
    printf '%s\n' "$*" >&2
    exit 1
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
    exec bash "$source_dir/install.sh"
fi

[[ -f "$target_dir/.env" && -f "$target_dir/bot.py" ]] ||
    fail "Укажите каталог существующего бота через --target или используйте ./install.sh."

if [[ "$target_dir" != "$source_dir" ]]; then
    install -m 0644 "$source_dir/"{bot.py,attachments.py,dialogues.py,access.py,evidence.py,redaction.py,credentials.py,encrypt-api-key.py,telegram_format.py,llmbot.service,llmbot-system.service,requirements.txt,HELP.md,VERSION,.env.example} "$target_dir/"
    install -m 0755 "$source_dir/"{install.sh,update.sh} "$target_dir/"
    for config in prompt.txt telegram-format.txt; do
        if [[ ! -e "$target_dir/$config" ]]; then
            install -m 0644 "$source_dir/$config" "$target_dir/$config"
        fi
    done
fi

if "$manual"; then
    exec bash "$target_dir/install.sh" --no-service
fi
exec bash "$target_dir/install.sh"
