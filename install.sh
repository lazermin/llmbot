#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
project_dir="$source_dir"
env_file="$source_dir/.env"
service=llmbot.service
setup_service=true
enable_linger=false
system_service=false
unit_path="/etc/systemd/system/$service"
user_unit=""
backup_dir=""
code_backup=""
changes_made=false
old_unit=false
old_user_unit=false
code_files=(bot.py telegram_format.py attachments.py sandbox.py dialogues.py access.py
    evidence.py redaction.py credentials.py encrypt-api-key.py requirements.txt VERSION)

fail() {
    printf '%s\n' "$*" >&2
    # Если код уже заменён, явный fail тоже должен откатить установку.
    if [[ "$changes_made" == true && -n "$backup_dir" && -d "$backup_dir" ]] && "$system_service"; then
        rollback_system
    fi
    exit 1
}

for option in "$@"; do
    case "$option" in
        --no-service) setup_service=false ;;
        --linger) enable_linger=true ;;
        -h|--help)
            cat <<'EOF'
Использование: ./install.sh [--no-service | --linger]
  От root создаётся системный сервис, от обычного пользователя — пользовательский.
  --no-service  Установить зависимости и настроить .env для ручного запуска.
  --linger      Включить запуск пользовательского сервиса до входа в систему.
LLMBOT_PYTHON позволяет указать путь к установленному Python 3.10 или новее.
EOF
            exit 0
            ;;
        *) fail "Неизвестный параметр: $option. См. ./install.sh --help." ;;
    esac
done

if ! "$setup_service" && "$enable_linger"; then
    fail "Параметры --no-service и --linger несовместимы."
fi
if [[ "$EUID" -eq 0 ]] && "$setup_service"; then
    if "$enable_linger"; then
        fail "Системный сервис запускается при загрузке. Повторите команду без --linger."
    fi
    system_service=true
    project_dir=/opt/llmbot
    env_file=/etc/llmbot/.env
fi
venv_python="$project_dir/.venv/bin/python"

as_root() {
    if [[ "$EUID" -eq 0 ]]; then
        "$@"
    else
        sudo "$@"
    fi
}

rollback_system() {
    trap - ERR
    if [[ "$changes_made" != true ]]; then
        [[ -n "$backup_dir" ]] && rm -rf -- "$backup_dir"
        exit 1
    fi
    set +e
    systemctl stop "$service" >/dev/null 2>&1
    if [[ -n "$code_backup" && -d "$code_backup" ]]; then
        cp -a -- "$code_backup/." "$project_dir/" >/dev/null 2>&1
    fi
    if [[ "$old_unit" == true ]]; then
        install -m 0644 -- "$backup_dir/unit" "$unit_path" >/dev/null 2>&1
    else
        rm -f -- "$unit_path"
    fi
    systemctl daemon-reload >/dev/null 2>&1
    if [[ "$old_unit" == true ]]; then
        systemctl restart "$service" >/dev/null 2>&1
    fi
    rm -rf -- "$backup_dir"
    if [[ "$old_unit" == true || -n "$code_backup" ]]; then
        printf '%s\n' 'Установка прервана. Предыдущая версия восстановлена; проверьте systemctl status llmbot.service.' >&2
    else
        printf '%s\n' 'Установка прервана. Изменения отменены.' >&2
    fi
    exit 1
}

rollback_user() {
    trap - ERR
    set +e
    systemctl --user stop "$service" >/dev/null 2>&1
    if [[ "$old_user_unit" == true ]]; then
        install -m 0644 -- "$backup_dir/unit" "$user_unit" >/dev/null 2>&1
    else
        rm -f -- "$user_unit"
    fi
    systemctl --user daemon-reload >/dev/null 2>&1
    if [[ "$old_user_unit" == true ]]; then
        systemctl --user restart "$service" >/dev/null 2>&1
    fi
    rm -rf -- "$backup_dir"
    printf '%s\n' 'Установка прервана. Конфигурация пользовательского сервиса восстановлена.' >&2
    exit 1
}

service_active() {
    local attempt state
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        state="$(systemctl is-active "$service" 2>/dev/null || true)"
        if [[ "$state" == active ]]; then
            sleep 1
            if systemctl is-active --quiet "$service"; then
                return 0
            fi
        elif [[ "$state" == failed ]]; then
            return 1
        fi
        sleep 1
    done
    return 1
}

user_service_active() {
    local attempt state
    for attempt in 1 2 3 4 5 6 7 8 9 10; do
        state="$(systemctl --user is-active "$service" 2>/dev/null || true)"
        if [[ "$state" == active ]]; then
            sleep 1
            if systemctl --user is-active --quiet "$service"; then
                return 0
            fi
        elif [[ "$state" == failed ]]; then
            return 1
        fi
        sleep 1
    done
    return 1
}

usable_python() {
    command -v "$1" >/dev/null 2>&1 &&
        "$1" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1
}

find_python() {
    local candidate
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
        if usable_python "$candidate"; then
            command -v "$candidate"
            return
        fi
    done
    return 1
}

install_python_packages() {
    local interpreter="${1:-}" rpm_manager minor package
    if [[ "$EUID" -ne 0 ]]; then
        command -v sudo >/dev/null 2>&1 ||
            fail "Для установки системных пакетов нужен sudo. Установите Python 3.10+ с поддержкой venv вручную."
    fi

    if command -v apt-get >/dev/null 2>&1; then
        as_root apt-get update || return 1
        if [[ -z "$interpreter" ]]; then
            as_root apt-get install -y python3 python3-venv ca-certificates
        else
            minor="$("$interpreter" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
            as_root apt-get install -y "python${minor}-venv" ca-certificates
        fi
        return
    fi

    if command -v dnf >/dev/null 2>&1; then
        rpm_manager=dnf
    elif command -v yum >/dev/null 2>&1; then
        rpm_manager=yum
    else
        fail "Установите Python 3.10+ с поддержкой venv средствами вашего дистрибутива."
    fi

    if [[ -n "$interpreter" ]]; then
        minor="$("$interpreter" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
        if [[ "${interpreter##*/}" == python3 ]]; then
            as_root "$rpm_manager" install -y python3-pip ca-certificates && return
        fi
        as_root "$rpm_manager" install -y "python${minor}-pip" ca-certificates
        return
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        as_root "$rpm_manager" install -y python3 python3-pip ca-certificates || return 1
        find_python >/dev/null && return 0
    fi

    # Если системный python3 слишком стар, пробуем версионные пакеты.
    for package in python3.11 python3.12; do
        as_root "$rpm_manager" install -y "$package" "$package-pip" ca-certificates && return
    done
    return 1
}

dotenv_line() {
    local value="$2"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '%s="%s"\n' "$1" "$value"
}

configure_env() (
    local token owner_id choice base_url model api_key default_url default_model default_key=
    local temp_env
    umask 077
    if [[ ! -t 0 ]]; then
        fail "Создайте .env из .env.example, заполните его и повторите запуск."
    fi
    read -r -s -p "BOT_TOKEN от @BotFather (ввод скрыт): " token
    printf '\n'
    read -r -p "ADMIN_ID (числовой Telegram ID владельца): " owner_id
    [[ "$token" =~ ^[0-9]+:[a-zA-Z0-9_-]+$ ]] || fail "Проверьте формат BOT_TOKEN."
    [[ "$owner_id" =~ ^[1-9][0-9]*$ ]] || fail "ADMIN_ID должен быть положительным числом."

    printf '%s\n' '1) DeepSeek' '2) OpenRouter' '3) Локальная Ollama' '4) Другой OpenAI-совместимый API'
    read -r -p "Провайдер [1]: " choice
    case "${choice:-1}" in
        1) default_url=https://api.deepseek.com; default_model=deepseek-flash ;;
        2) default_url=https://openrouter.ai/api/v1; default_model=deepseek/deepseek-chat ;;
        3) default_url=http://127.0.0.1:11434/v1; default_model=qwen2.5:14b; default_key=ollama ;;
        4) default_url=; default_model= ;;
        *) fail "Выберите провайдера от 1 до 4." ;;
    esac
    read -r -p "LLM_BASE_URL [${default_url}]: " base_url
    read -r -p "LLM_MODEL [${default_model}]: " model
    read -r -s -p "LLM_API_KEY [${default_key:-ввод скрыт}]: " api_key
    printf '\n'
    base_url="${base_url:-$default_url}"
    model="${model:-$default_model}"
    api_key="${api_key:-$default_key}"
    [[ -n "$base_url" && -n "$model" && -n "$api_key" ]] ||
        fail "Заполните настройки LLM_BASE_URL, LLM_MODEL и LLM_API_KEY."

    temp_env="$(mktemp "${env_file}.XXXXXXXX")"
    trap 'rm -f -- "$temp_env"' EXIT
    {
        dotenv_line BOT_TOKEN "$token"
        dotenv_line ADMIN_ID "$owner_id"
        dotenv_line LLM_BASE_URL "$base_url"
        dotenv_line LLM_MODEL "$model"
        dotenv_line LLM_API_KEY "$api_key"
        printf '%s\n' 'LLM_TIMEOUT=120' 'HISTORY_TURNS=4' 'LLM_WEB_TOOLS=auto' 'LLM_VISION_MODEL=auto'
    } > "$temp_env"
    # Не затираем .env, если его одновременно создали другим способом.
    ln -- "$temp_env" "$env_file"
)

prepare_system_install() {
    local account account_uid account_home account_shell file
    systemctl show-environment >/dev/null ||
        fail "Системный systemd недоступен. Для ручного запуска используйте --no-service."

    # Резервная копия заменяемых файлов: при сбое установки вернём как было.
    if [[ -d "$project_dir" ]]; then
        code_backup="$backup_dir/code"
        install -d -m 0700 -- "$code_backup"
        for file in "${code_files[@]}"; do
            if [[ -f "$project_dir/$file" ]]; then
                cp -a -- "$project_dir/$file" "$code_backup/$file"
            fi
        done
    fi
    if [[ -f "$unit_path" ]]; then
        cp -a -- "$unit_path" "$backup_dir/unit"
        old_unit=true
    fi

    if account="$(getent passwd llmbot)"; then
        IFS=: read -r _ _ account_uid _ _ account_home account_shell <<< "$account"
        [[ "$account_uid" != 0 && "$account_home" == "$project_dir" && "${account_shell##*/}" == nologin ]] ||
            fail "Имя llmbot уже занято другой учётной записью."
    else
        useradd --system --user-group --home-dir "$project_dir" --no-create-home --shell /sbin/nologin llmbot
    fi
    install -d -o root -g root -m 0755 "$project_dir"
    install -d -o root -g llmbot -m 0750 "${env_file%/*}"
    if [[ ! -e "$env_file" && -f "$source_dir/.env" ]]; then
        install -o root -g llmbot -m 0640 "$source_dir/.env" "$env_file"
    fi
    local format_file="${env_file%/*}/telegram-format.txt"
    if [[ ! -e "$format_file" ]]; then
        install -o root -g llmbot -m 0640 "$source_dir/telegram-format.txt" "$format_file"
    fi
    if [[ "$source_dir" != "$project_dir" ]]; then
        for file in "${code_files[@]}"; do
            install -m 0644 -- "$source_dir/$file" "$project_dir/$file"
        done
        changes_made=true
        if [[ ! -e "$project_dir/prompt.txt" ]]; then
            install -m 0644 -- "$source_dir/prompt.txt" "$project_dir/prompt.txt"
        fi
    fi
}

if "$system_service"; then
    backup_dir="$(mktemp -d)"
    trap 'rollback_system' ERR
    # Код и виртуальное окружение должны быть доступны сервису при любом umask root.
    umask 022
    prepare_system_install
fi
[[ -f "$env_file" ]] || configure_env
if "$system_service"; then
    chown root:llmbot "$env_file"
    chmod 640 "$env_file"
else
    chmod 600 "$env_file"
fi

if ! usable_python "$venv_python" || ! "$venv_python" -m pip --version >/dev/null 2>&1; then
    if [[ -n "${LLMBOT_PYTHON:-}" ]]; then
        usable_python "$LLMBOT_PYTHON" || fail "LLMBOT_PYTHON должен указывать на Python 3.10 или новее."
        python_bin="$(command -v "$LLMBOT_PYTHON")"
    elif ! python_bin="$(find_python)"; then
        install_python_packages || fail "Не удалось установить Python через системный менеджер пакетов."
        python_bin="$(find_python)" || fail "Требуется Python 3.10 или новее из доступного репозитория."
    fi

    if ! "$python_bin" -m venv "$project_dir/.venv"; then
        install_python_packages "$python_bin" || fail "Не удалось установить пакет для Python venv."
        "$python_bin" -m venv "$project_dir/.venv" ||
            fail "Не удалось создать .venv. Проверьте выбранный Python и пакет для venv."
    fi
fi

"$venv_python" -m pip install -r "$project_dir/requirements.txt"

if "$system_service"; then
    if [[ -f "${env_file%/*}/llm-api-key.cred" || -f "/etc/systemd/system/$service.d/40-llm-api-key.conf" ]]; then
        "$venv_python" "$project_dir/encrypt-api-key.py" --check-service
    else
        (
            cd -- "$project_dir"
            runuser -u llmbot -g llmbot -- env -i LLMBOT_ENV_FILE="$env_file" "$venv_python" "$project_dir/bot.py" --check-config
        )
    fi
    install -m 0644 -- "$source_dir/llmbot-system.service" "$unit_path"
    changes_made=true
    systemctl daemon-reload
    systemctl restart "$service"
    if ! service_active; then
        printf '%s\n' 'Сервис llmbot.service не запустился. Восстанавливаю предыдущую версию.' >&2
        rollback_system
    fi
    systemctl enable "$service"
    trap - ERR
    rm -rf -- "$backup_dir"
    printf '%s\n' 'Конфигурация: /etc/llmbot/.env' \
        'Статус: systemctl status llmbot.service' \
        'Журнал: journalctl -u llmbot.service -f'
else
    LLMBOT_ENV_FILE="$env_file" "$venv_python" "$project_dir/bot.py" --check-config
    printf 'Ручной запуск: %q %q\n' "$venv_python" "$project_dir/bot.py"
fi

if "$setup_service" && ! "$system_service"; then
    systemctl --user show-environment >/dev/null ||
        fail "Пользовательский systemd недоступен. Войдите в полноценную сессию SSH или повторите с --no-service."

    unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$unit_dir"
    user_unit="$unit_dir/$service"
    unit="$(< "$project_dir/llmbot.service")"
    escaped_project="${project_dir//\\/\\\\}"
    escaped_project="${escaped_project//\"/\\\"}"
    escaped_project="${escaped_project//%/%%}"
    [[ "$escaped_project" != *$'\n'* && "$escaped_project" != *$'\r'* ]] ||
        fail "Для systemd используйте путь проекта без переводов строк."
    backup_dir="$(mktemp -d)"
    old_user_unit=false
    if [[ -f "$user_unit" ]]; then
        cp -a -- "$user_unit" "$backup_dir/unit"
        old_user_unit=true
    fi
    trap 'rollback_user' ERR
    printf '%s\n' "${unit//@PROJECT_DIR@/"$escaped_project"}" > "$user_unit"
    systemctl --user daemon-reload
    systemctl --user restart "$service"
    if ! user_service_active; then
        printf '%s\n' 'Пользовательский сервис llmbot.service не запустился. Восстанавливаю предыдущую конфигурацию.' >&2
        rollback_user
    fi
    systemctl --user enable "$service"
    if "$enable_linger"; then
        if ! loginctl enable-linger "$(id -un)"; then
            as_root loginctl enable-linger "$(id -un)" ||
                printf '%s\n' 'Не удалось включить linger; сервис будет работать только при входе в систему.' >&2
        fi
    fi
    trap - ERR
    rm -rf -- "$backup_dir"
    printf '%s\n' 'Статус: systemctl --user status llmbot.service' \
        'Журнал: journalctl --user -u llmbot.service -f'
fi

printf '%s\n' 'Установка завершена. Отправьте /start боту в личном чате.'
