# envsleuth

🌐 [English](../README.md) · [简体中文](README.zh-CN.md) · **Русский**

![README: generated with AI](https://img.shields.io/badge/README-generated%20with%20AI-6f42c1)

[![tests](https://github.com/k38f/envsleuth/actions/workflows/tests.yml/badge.svg)](https://github.com/k38f/envsleuth/actions/workflows/tests.yml)
[![pypi](https://img.shields.io/pypi/v/envsleuth.svg)](https://pypi.org/project/envsleuth/)
[![python](https://img.shields.io/pypi/pyversions/envsleuth.svg)](https://pypi.org/project/envsleuth/)
[![license](https://img.shields.io/pypi/l/envsleuth.svg)](../LICENSE)

`envsleuth` разбирает Python-код через AST, находит обращения к окружению через
стандартную библиотеку, pydantic-settings, django-environ и python-decouple,
затем сверяет их с одним или несколькими `.env`. Проверяемый проект при этом
не импортируется и не выполняется.

![Демонстрация envsleuth](../demo.gif)

## Установка

Требуется Python 3.10 или новее.

```bash
python -m pip install envsleuth
```

## Использование

```bash
# сканировать текущую папку и свериться с ./.env
envsleuth scan

# указать папку и env-файл
envsleuth scan --path ./src --env .env.production

# независимо проверить несколько профилей за одно сканирование
envsleuth scan --env .env.development --env .env.production

# режим CI — код выхода 1, если чего-то не хватает
envsleuth scan --strict

# отдельно выбрать категории, на которых CI должен падать
envsleuth scan --fail-on missing --fail-on dynamic

# создать .env.example по коду
envsleuth generate

# JSON или SARIF 2.1.0
envsleuth scan --json
envsleuth scan --output sarif > envsleuth.sarif
```

### Пример вывода

```
Found 6 variables in code
checking against .env

⚠️  AWS_SECRET — not in .env but has default in code (probably ok)
✅ DATABASE_URL
✅ DEBUG
❌ REDIS_URL — missing from .env
     at src/app.py:7
✅ SECRET_KEY
❌ STRIPE_API_KEY — missing from .env
     at src/app.py:6

⚠️  1 dynamic usage (variable name computed at runtime, can't check statically)
     src/app.py:12  →  getenv(name)

ℹ  1 variable in .env not referenced in code: UNUSED_VAR

3 ok  1 with default  2 missing
```

## Поддерживаемые обращения

Поддерживаются три распространённых шаблона:

```python
import os

a = os.getenv("A")              # обязательная — должна быть в .env
b = os.getenv("B", "fallback")  # есть значение по умолчанию — будет предупреждение
c = os.environ["C"]             # обязательная (без неё будет KeyError)
d = os.environ.get("D")         # обязательная
```

Также поддерживаются импорты с псевдонимами:

```python
from os import getenv, environ
import os as sys_os

a = getenv("A")
b = environ["B"]
c = sys_os.getenv("C")
```

Переменные, имена которых вычисляются во время выполнения (например, `os.getenv(f"PREFIX_{x}")`), нельзя проверить статически. Они выводятся в отдельном блоке предупреждений.

### Pydantic settings

Классы `BaseSettings` анализируются статически, поэтому Pydantic не становится
зависимостью envsleuth:

```python
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")

    database_url: str
    timeout: int = 10
    token: str = Field(
        validation_alias=AliasChoices("TOKEN", "LEGACY_TOKEN")
    )
```

Поддерживаются литеральный `env_prefix`, `env_prefix_target`, `alias`,
`validation_alias`, `AliasChoices`, обычные значения по умолчанию,
`default_factory` и локальное наследование settings-классов. Вычисляемые
конфиги, распаковки и генераторы alias не угадываются, а показываются как
динамические обращения.

Пользовательские settings sources, runtime-переопределения `_env_prefix` и
`_case_sensitive`, наследование между модулями и разворачивание nested
delimiter нельзя доказать по AST одного модуля — такие случаи нужно проверить
вручную.

### Django и библиотеки конфигурации

Также поддерживаются django-environ и python-decouple:

```python
# django-environ
import environ
env = environ.Env()
SECRET_KEY = env('SECRET_KEY')
DEBUG = env.bool('DEBUG', default=False)
DATABASES = {'default': env.db('DATABASE_URL')}
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=[])

# python-decouple
from decouple import config
SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', default=False, cast=bool)
```

Распознаются обращения через `env(...)`, `env.get_value(...)` и типизированные
методы: `str`, `bytes`, `bool`, `int`, `float`, `json`, `list`, `tuple`, `dict`,
`url`, `db_url`/`db`, `cache_url`/`cache`, `email_url`/`email`, `search_url`,
`channels_url`/`channels` и `path`. Также поддерживаются `FileAwareEnv` и
`Env.configured(...)`, включая значения по умолчанию из их схем и статически
известные настройки `env.prefix`. Импорты с псевдонимами тоже работают:
`from decouple import config as cfg`.

## CI: аннотации GitHub Actions

Режим `--output github` выводит отсутствующие переменные как аннотации к строкам исходного кода:

```yaml
# .github/workflows/env-check.yml
- name: Check env vars
  run: envsleuth scan --output github --strict
```

Каждая отсутствующая переменная становится аннотацией `::error`, а динамические обращения — `::warning`. Формат соответствует [спецификации workflow commands](https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions) GitHub.

Для GitHub Code Scanning можно сформировать SARIF:

```yaml
permissions:
  contents: read
  security-events: write

steps:
  - name: Analyze environment configuration
    run: envsleuth scan --output sarif --fail-on missing > envsleuth.sarif

  - uses: github/codeql-action/upload-sarif@v4
    if: always()
    with:
      sarif_file: envsleuth.sarif
```

SARIF детерминирован, использует стабильные идентификаторы правил и никогда не
включает значения из `.env`.

## pre-commit hook

Добавьте envsleuth в `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/k38f/envsleuth
    rev: v1.0.0
    hooks:
      - id: envsleuth
        # необязательные переопределения
        # args: [--path, src, --env, .env]
```

`envsleuth scan --strict` запускается при изменении Python-файлов, `.env`,
`.env.*` или `.envignore`. Дополнительный hook `envsleuth-generate` можно
запустить вручную для пересоздания `.env.example`:
`pre-commit run envsleuth-generate --hook-stage manual`.

## `envsleuth generate`

Сканирует код и записывает в `.env.example` все найденные переменные, комментарии с местами их использования и значения по умолчанию, если они есть:

```bash
$ envsleuth generate
Wrote 6 variables to .env.example

$ cat .env.example
# Generated by envsleuth — edit this file before committing.
# Each variable below is used somewhere in your code.

# used at src/app.py:8
AWS_SECRET=default-value

# used at src/app.py:3
DATABASE_URL=

# used at src/app.py:5
DEBUG=false
...
```

`--force` перезаписывает существующий файл, а `--output path/to/file` указывает другое место записи.

Генерация работает по принципу fail-closed: если какой-либо исходный файл не
удалось просканировать или имя переменной нельзя записать как переносимое
присваивание окружения, команда завершается с кодом 2 и не создаёт и не
перезаписывает целевой файл даже с `--force`. Динамические обращения сохраняются
в предупреждающих комментариях. Литеральное значение по умолчанию записывается,
только если оно одинаково читается `python-dotenv` и POSIX shell; иначе значение
остаётся пустым с пометкой `# default omitted`.
В Windows генерация также отклоняет имена, отличающиеся только регистром
(например, `FOO` и `foo`), потому что окружение Windows не может хранить их
раздельно.

## `.envignore`

Исключайте переменные из проверки на отсутствие с помощью glob-шаблонов — по одному на строку:

```
# .envignore
TEST_*
LEGACY_*
DEBUG_TOOL
```

Это подходит для переменных из CI, Docker или shell-конфига, которых нет в локальном `.env`.

## Конфигурация проекта

Общие параметры можно хранить в ближайшем `pyproject.toml`:

```toml
[tool.envsleuth]
path = "src"
env = [".env.development", ".env.production"]
envignore = ".envignore"
fail-on = ["missing", "dynamic"]
exclude = ["vendor", "generated"]
extensions = [".pyi"]
```

Пути из таблицы считаются от `pyproject.toml`. Для безопасности пути в
автоматически найденном конфиге не могут выходить из его каталога; явно
выбранный `--config FILE` разрешает такие внешние пути осознанно. Переданные
через CLI пути считаются от текущей папки и имеют приоритет. `--no-config`
отключает поиск. Неизвестные ключи и неверные типы завершают команду с ошибкой,
поэтому опечатки не игнорируются молча.

`--strict` по-прежнему равен политике `--fail-on missing`. `--no-fail-on`
очищает только список `fail-on`; чтобы также отменить `strict = true`, передайте
`--no-strict`.

## Справочник CLI

### `envsleuth scan`

| Флаг | Описание |
| --- | --- |
| `--path`, `-p` | Папка или файл. По умолчанию: корень конфига либо `.` |
| `--env` | Env-файл; можно повторять для независимых профилей |
| `--envignore` | Путь к `.envignore`. По умолчанию: `./.envignore`, если файл есть |
| `--strict`, `--no-strict` | Включить/выключить ошибку на missing |
| `--fail-on CATEGORY` | Ошибка на `missing`, `extra` или `dynamic`; можно повторять |
| `--no-fail-on` | Очистить список `fail-on` из конфигурации |
| `--output`, `-o` | `text`, `json`, `github` или `sarif` |
| `--json` | Псевдоним `--output json`, сохранённый для обратной совместимости |
| `--no-color` | Отключить ANSI-цвета; также учитывает `NO_COLOR` |
| `--exclude DIR` | Дополнительное имя папки, которую нужно пропустить. Можно повторять |
| `--ext .EXT` | Дополнительное расширение файлов, например `.pyi`. Можно повторять |
| `--verbose`, `-v` | Показать места использования каждой переменной |
| `--no-update-check` | Пропустить еженедельную проверку версии на PyPI |
| `--config FILE`, `--no-config` | Выбрать или отключить TOML-конфиг |

### `envsleuth generate`

| Флаг | Описание |
| --- | --- |
| `--path`, `-p` | Папка или файл. По умолчанию: путь/корень конфига либо `.` |
| `--output`, `-o` | Куда записать файл. По умолчанию: `./.env.example` |
| `--force`, `-f` | Перезаписать существующий файл |
| `--no-color` | Отключить ANSI-цвета в сообщении об успехе |
| `--exclude`, `--ext` | То же, что в `scan` |
| `--no-update-check` | Пропустить еженедельную проверку версии на PyPI |
| `--config FILE`, `--no-config` | Выбрать или отключить конфиг проекта |

### Коды выхода

- `0` — команда выполнена успешно.
- `1` — найдена категория, выбранная через `--strict` или `--fail-on`.
- `2` — операционная ошибка: например, отсутствует `.env`, сканирование
  завершилось не полностью, неверен конфиг или путь, произошла ошибка чтения,
  записи либо генерации. JSON, GitHub и SARIF по возможности возвращают один
  корректный структурированный документ.

## Уведомления об обновлениях

envsleuth не чаще раза в неделю проверяет наличие новых выпусков на PyPI. Если новая версия есть, в stderr выводится одна строка:

```
ℹ  envsleuth 1.0.1 is available (you have 1.0.0). Run: python -m pip install --upgrade envsleuth
```

Результат кэшируется, запрос имеет короткий тайм-аут, а при любой ошибке (нет сети, доступ заблокирован и т. д.) проверка молча завершается. Чтобы полностью её отключить:

```bash
# для одной команды
envsleuth scan --no-update-check

# для всей shell-сессии
export ENVSLEUTH_NO_UPDATE_CHECK=1
```

Кэш находится в `~/.cache/envsleuth/last_check.json` (или `$XDG_CACHE_HOME/envsleuth/...`).

## Сравнение

| | envsleuth | [dotenv-linter](https://github.com/dotenv-linter/dotenv-linter) | [python-decouple](https://github.com/HBNetwork/python-decouple) |
| --- | --- | --- | --- |
| Сканирует **код** на использование env-переменных | ✅ | ❌ | ❌ |
| Проверяет **сам файл .env** | ❌ | ✅ | ❌ |
| Читает конфиг в runtime с приведением типов | ❌ | ❌ | ✅ |
| Создаёт `.env.example` по коду | ✅ | ❌ | ❌ |
| Язык | Python | Rust | Python |

Эти инструменты решают разные задачи: envsleuth сканирует исходный код,
dotenv-linter проверяет `.env`, а python-decouple читает конфигурацию во время работы программы.

## Зависимости

- [click](https://click.palletsprojects.com/) — CLI
- [python-dotenv](https://github.com/theskumar/python-dotenv) — разбор `.env`
- [flashbar](https://github.com/k38f/flashbar) — индикатор прогресса при сканировании 20+ файлов
- [packaging](https://packaging.pypa.io/) — сравнение версий по PEP 440 при проверке обновлений
- [tomli](https://github.com/hukkin/tomli) — единый TOML-парсер для всех поддерживаемых версий Python

Сам сканер использует только стандартную библиотеку Python (`ast`); Pydantic,
django-environ и python-decouple распознаются без установки.

## Лицензия

MIT
