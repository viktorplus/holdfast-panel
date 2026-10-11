"""The templates that ship with the panel.

Written as plain dicts in the same shape as a user's JSON file, so a user can
copy one into the library and edit it. Every step is checked by the tests: the
template parses, renders, and each shell line passes `bash -n`.
"""

from __future__ import annotations

from .templates import Template, parse

_PASSWORD = "[A-Za-z0-9_.-]{8,}"
_LABEL = "[A-Za-z0-9][A-Za-z0-9._-]*"

BOOTSTRAP = {
    "id": "holdfast-bootstrap",
    "name": "Первичная установка сервера (holdfast)",
    "category": "setup",
    "description": (
        "Обновляет систему, ставит пакеты, Docker и holdfast, настраивает машину, "
        "записывает публичный ключ шифрования и включает ночной бэкап."
    ),
    "variables": [
        {"name": "host_label", "label": "Имя машины (host-label)", "pattern": _LABEL},
        {
            "name": "recipient",
            "label": "Публичный ключ age (age1…)",
            "pattern": "age1[a-z0-9]+",
        },
        {
            "name": "backup_mode",
            "label": "Режим бэкапа (auto или manual)",
            "default": "auto",
            "pattern": "auto|manual",
        },
        {
            "name": "install_docker",
            "label": "Ставить Docker (yes или no)",
            "default": "yes",
            "pattern": "yes|no",
        },
        {
            "name": "cron_hour",
            "label": "Час ночного бэкапа (0-23)",
            "default": "2",
            "pattern": "[01]?[0-9]|2[0-3]",
        },
        {
            "name": "repo",
            "label": "Репозиторий holdfast",
            "default": "https://github.com/viktorplus/holdfast.git",
            "pattern": "https://[^\\s'\"]+",
        },
    ],
    "steps": [
        {
            "title": "Обновить пакеты",
            "run": "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update && apt-get -y upgrade",
        },
        {
            "title": "Поставить python3, zstd, age, git, curl",
            "run": "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get install -y python3 python3-venv zstd age git curl",
        },
        {
            "title": "Поставить Docker",
            "run": "if [ {{install_docker}} = yes ] && ! command -v docker >/dev/null; "
            "then curl -fsSL https://get.docker.com | sh; fi",
        },
        {
            "title": "Скачать holdfast",
            "run": "if [ -d /root/holdfast/.git ]; then "
            "git -C /root/holdfast pull --ff-only; "
            "else git clone {{repo}} /root/holdfast; fi",
        },
        {
            "title": "Установить holdfast",
            "run": "python3 -m venv /opt/holdfast && "
            "/opt/holdfast/bin/pip install -q /root/holdfast && "
            "ln -sf /opt/holdfast/bin/holdfast /usr/local/bin/holdfast && "
            "holdfast version",
        },
        {
            "title": "Настроить машину (holdfast init)",
            "run": "[ -f /etc/holdfast/holdfast.toml ] || "
            "holdfast init --fresh --host-label {{host_label}} "
            "--backup-mode {{backup_mode}}",
        },
        {
            "title": "Записать публичный ключ шифрования",
            "run": 'key={{recipient}}; sed -i "s|^recipients = .*|recipients = '
            '[\\"$key\\"]|" /etc/holdfast/holdfast.toml && '
            'grep -q "$key" /etc/holdfast/holdfast.toml',
        },
        {
            "title": "Расписание бэкапа (cron)",
            "kind": "file",
            "path": "/etc/cron.d/holdfast",
            "mode": "644",
            "run": "SHELL=/bin/bash\nPATH=/usr/local/bin:/usr/bin:/bin\n\n"
            "30 {{cron_hour}} * * * root /usr/local/bin/holdfast backup\n"
            "0 7 * * * root /usr/local/bin/holdfast audit > /dev/null\n",
        },
        {"title": "Проверить настройки", "run": "holdfast config check"},
        {
            "title": "Показать, что попадёт в копию",
            "run": "holdfast backup --dry-run",
            "ignore_failure": True,
        },
    ],
}

SYSTEM_UPDATE = {
    "id": "system-update",
    "name": "Обновление системы",
    "category": "maintenance",
    "description": "apt update и upgrade; сообщает, нужна ли перезагрузка.",
    "steps": [
        {
            "title": "Обновить пакеты",
            "run": "export DEBIAN_FRONTEND=noninteractive; "
            "apt-get update && apt-get -y upgrade && apt-get -y autoremove",
        },
        {
            "title": "Нужна ли перезагрузка",
            "run": "if [ -f /var/run/reboot-required ]; then "
            "echo 'ТРЕБУЕТСЯ ПЕРЕЗАГРУЗКА'; else echo 'перезагрузка не нужна'; fi",
        },
    ],
}

FAIL2BAN = {
    "id": "fail2ban",
    "name": "Защита SSH (fail2ban)",
    "category": "security",
    "description": "Ставит fail2ban и включает его; блокирует подбор паролей.",
    "steps": [
        {
            "title": "Установить fail2ban",
            "run": "export DEBIAN_FRONTEND=noninteractive; apt-get install -y fail2ban",
        },
        {
            "title": "Включить",
            "run": "systemctl enable --now fail2ban && systemctl is-active fail2ban",
        },
    ],
}

UFW = {
    "id": "firewall-ufw",
    "name": "Файрвол (ufw)",
    "category": "security",
    "description": "Открывает SSH, 80 и 443, остальное закрывает. "
    "Порт SSH нужно указать верно, иначе вы потеряете доступ.",
    "variables": [
        {"name": "ssh_port", "label": "Порт SSH", "kind": "number", "default": "22"}
    ],
    "steps": [
        {
            "title": "Установить ufw",
            "run": "export DEBIAN_FRONTEND=noninteractive; apt-get install -y ufw",
        },
        {"title": "Разрешить SSH", "run": "ufw allow {{ssh_port}}/tcp"},
        {"title": "Разрешить 80 и 443", "run": "ufw allow 80/tcp && ufw allow 443/tcp"},
        {
            "title": "Включить файрвол",
            "run": "ufw --force enable && ufw status",
            "dangerous": True,
        },
    ],
}

WORDPRESS = {
    "id": "docker-wordpress",
    "name": "WordPress + MySQL (Docker)",
    "category": "docker",
    "description": "Проект в /root/<имя>: данные сайта и базы лежат папками внутри "
    "проекта, поэтому попадают в бэкап без безымянных томов.",
    "variables": [
        {"name": "project", "label": "Имя проекта", "pattern": "[a-z][a-z0-9_-]*"},
        {"name": "port", "label": "Порт сайта", "kind": "number", "default": "8080"},
        {
            "name": "db_password",
            "label": "Пароль базы (от 8 знаков: буквы, цифры, _ . -)",
            "kind": "password",
            "pattern": _PASSWORD,
        },
        {
            "name": "root_password",
            "label": "Пароль root MySQL (от 8 знаков)",
            "kind": "password",
            "pattern": _PASSWORD,
        },
    ],
    "steps": [
        {
            "title": "Записать docker-compose.yml",
            "kind": "file",
            "path": "/root/{{project}}/docker-compose.yml",
            "mode": "600",
            "run": "services:\n"
            "  db:\n"
            "    image: mysql:8.4\n"
            "    restart: unless-stopped\n"
            "    environment:\n"
            '      MYSQL_ROOT_PASSWORD: "{{root_password}}"\n'
            "      MYSQL_DATABASE: wordpress\n"
            "      MYSQL_USER: wordpress\n"
            '      MYSQL_PASSWORD: "{{db_password}}"\n'
            "    volumes:\n"
            "      - ./db_data:/var/lib/mysql\n"
            "  wordpress:\n"
            "    image: wordpress:latest\n"
            "    restart: unless-stopped\n"
            "    depends_on:\n"
            "      - db\n"
            "    ports:\n"
            '      - "{{port}}:80"\n'
            "    environment:\n"
            "      WORDPRESS_DB_HOST: db\n"
            "      WORDPRESS_DB_USER: wordpress\n"
            '      WORDPRESS_DB_PASSWORD: "{{db_password}}"\n'
            "      WORDPRESS_DB_NAME: wordpress\n"
            "    volumes:\n"
            "      - ./wp_data:/var/www/html\n",
        },
        {
            "title": "Запустить",
            "run": "cd /root/{{project}} && docker compose -p {{project}} up -d",
        },
    ],
}

POSTGRES = {
    "id": "docker-postgres",
    "name": "PostgreSQL (Docker)",
    "category": "docker",
    "description": "PostgreSQL 16, порт только на localhost, данные папкой в проекте.",
    "variables": [
        {"name": "project", "label": "Имя проекта", "pattern": "[a-z][a-z0-9_-]*"},
        {"name": "port", "label": "Порт", "kind": "number", "default": "5432"},
        {
            "name": "password",
            "label": "Пароль postgres (от 8 знаков)",
            "kind": "password",
            "pattern": _PASSWORD,
        },
    ],
    "steps": [
        {
            "title": "Записать docker-compose.yml",
            "kind": "file",
            "path": "/root/{{project}}/docker-compose.yml",
            "mode": "600",
            "run": "services:\n"
            "  db:\n"
            "    image: postgres:16\n"
            "    restart: unless-stopped\n"
            "    environment:\n"
            '      POSTGRES_PASSWORD: "{{password}}"\n'
            "    ports:\n"
            '      - "127.0.0.1:{{port}}:5432"\n'
            "    volumes:\n"
            "      - ./pg_data:/var/lib/postgresql/data\n",
        },
        {
            "title": "Запустить",
            "run": "cd /root/{{project}} && docker compose -p {{project}} up -d",
        },
    ],
}

PORTAINER = {
    "id": "docker-portainer",
    "name": "Portainer (управление Docker)",
    "category": "docker",
    "description": "Веб-интерфейс Docker на https, порт 9443.",
    "steps": [
        {
            "title": "Запустить Portainer",
            "run": "docker volume create portainer_data >/dev/null && "
            "(docker rm -f portainer >/dev/null 2>&1; true) && "
            "docker run -d --name portainer --restart=always -p 9443:9443 "
            "-v /var/run/docker.sock:/var/run/docker.sock "
            "-v portainer_data:/data portainer/portainer-ce:latest",
        }
    ],
}

_ALL = [BOOTSTRAP, SYSTEM_UPDATE, FAIL2BAN, UFW, WORDPRESS, POSTGRES, PORTAINER]


def builtin_templates() -> list[Template]:
    return [parse(raw, builtin=True) for raw in _ALL]
