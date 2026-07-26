# envsleuth

🌐 [English](../README.md) · **简体中文** · [Русский](README.ru.md)

![README: generated with AI](https://img.shields.io/badge/README-generated%20with%20AI-6f42c1)

[![tests](https://github.com/k38f/envsleuth/actions/workflows/tests.yml/badge.svg)](https://github.com/k38f/envsleuth/actions/workflows/tests.yml)
[![pypi](https://img.shields.io/pypi/v/envsleuth.svg)](https://pypi.org/project/envsleuth/)
[![python](https://img.shields.io/pypi/pyversions/envsleuth.svg)](https://pypi.org/project/envsleuth/)
[![license](https://img.shields.io/pypi/l/envsleuth.svg)](../LICENSE)

`envsleuth` 使用 AST 解析 Python 源代码，识别标准库、
pydantic-settings、django-environ 和 python-decouple 中的环境变量，
然后与一个或多个 `.env` 文件比较。被扫描的项目不会被导入或执行。

![envsleuth 演示](../demo.gif)

## 安装

需要 Python 3.10 或更高版本。

```bash
python -m pip install envsleuth
```

## 使用方法

```bash
# 扫描当前目录，并与 ./.env 对照
envsleuth scan

# 指定目录和 env 文件
envsleuth scan --path ./src --env .env.production

# 一次扫描，独立检查多个部署配置
envsleuth scan --env .env.development --env .env.production

# CI 模式——如果有变量缺失，以状态码 1 退出
envsleuth scan --strict

# 精确选择会让 CI 失败的类别
envsleuth scan --fail-on missing --fail-on dynamic

# 根据代码生成 .env.example
envsleuth generate

# JSON 或 SARIF 2.1.0
envsleuth scan --json
envsleuth scan --output sarif > envsleuth.sarif
```

### 输出示例

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

## 能识别什么

支持三种常见用法：

```python
import os

a = os.getenv("A")              # 必需——必须在 .env 中
b = os.getenv("B", "fallback")  # 有默认值——会警告但不强制要求
c = os.environ["C"]             # 必需（否则会抛出 KeyError）
d = os.environ.get("D")         # 必需
```

也能处理别名导入：

```python
from os import getenv, environ
import os as sys_os

a = getenv("A")
b = environ["B"]
c = sys_os.getenv("C")
```

在运行时才能确定名称的变量（例如 `os.getenv(f"PREFIX_{x}")`）无法静态检查。它们会在单独的警告区块中列出，让你知道它们的存在。

### Pydantic settings

`BaseSettings` 类会被静态分析，不需要把 Pydantic 安装为 envsleuth
的运行时依赖：

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

支持静态 `env_prefix`、`env_prefix_target`、`alias`、
`validation_alias`、`AliasChoices`、默认值、`default_factory` 和本地
settings 类继承。计算配置、字典解包和 alias generator 会作为 dynamic
finding 报告，而不是被猜测。

自定义 settings source、运行时 `_env_prefix`/`_case_sensitive` 覆盖、
跨模块继承和 nested delimiter 展开无法仅从单个模块的 AST 证明，
这些框架特定情况需要手动检查。

### Django 和配置库

envsleuth 还能识别两种最常见的第三方配置模式：

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

可识别通过 `env(...)`、`env.get_value(...)` 和类型辅助方法进行的调用：
`str`、`bytes`、`bool`、`int`、`float`、`json`、`list`、`tuple`、`dict`、
`url`、`db_url`/`db`、`cache_url`/`cache`、`email_url`/`email`、`search_url`、
`channels_url`/`channels` 和 `path`。同时支持 `FileAwareEnv` 和
`Env.configured(...)`，包括其 schema 中声明的默认值和可静态确定的
`env.prefix` 设置。别名导入也可以正常工作，例如
`from decouple import config as cfg`。

## CI：GitHub Actions 注解

让缺失的环境变量直接显示为 PR 中对应源代码行的注解：

```yaml
# .github/workflows/env-check.yml
- name: Check env vars
  run: envsleuth scan --output github --strict
```

每个缺失的变量都会生成一条 `::error` 注解，动态查找则会生成 `::warning`。该格式遵循 GitHub 的 [workflow command 规范](https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions)。

GitHub Code Scanning 可使用 `envsleuth scan --output sarif`。SARIF 2.1.0
输出是确定性的，使用稳定规则 ID，并且不会包含 `.env` 值或源码片段。

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

## pre-commit hook

将 envsleuth 添加到 `.pre-commit-config.yaml`：

```yaml
repos:
  - repo: https://github.com/k38f/envsleuth
    rev: v1.0.0
    hooks:
      - id: envsleuth
        # 可选的自定义参数
        # args: [--path, src, --env, .env]
```

当 Python 文件、`.env`、`.env.*` 或 `.envignore` 发生变化时，会运行
`envsleuth scan --strict`。另外还提供可选的 `envsleuth-generate` hook，
可通过 `pre-commit run envsleuth-generate --hook-stage manual` 手动重新生成
`.env.example`。

## `envsleuth generate`

扫描代码并生成 `.env.example`，其中包含所有找到的变量、指向使用位置的注释，以及代码中设定的默认值（如果有）：

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

使用 `--force` 覆盖现有文件，使用 `--output path/to/file` 写入其他位置。

生成过程采用 fail-closed 策略：如果任何源文件无法扫描，或变量名无法写成
可移植的环境变量赋值，命令会以状态码 2 退出，并且不会创建或覆盖目标文件，
即使使用了 `--force`。动态查找会保留为警告注释。只有能被 `python-dotenv`
和 POSIX shell 一致解析的字面量默认值才会写入；否则该值会留空，并附带
`# default omitted` 注释。
在 Windows 上，仅大小写不同的变量名（例如 `FOO` 和 `foo`）也会被拒绝，
因为 Windows 环境无法将它们作为两个独立变量保存。

## `.envignore`

使用 glob 模式将变量排除在“缺失”检查之外，每行一个：

```
# .envignore
TEST_*
LEGACY_*
DEBUG_TOOL
```

适用于那些来自 CI、Docker 或 shell 配置文件，而不是本地 `.env` 的变量。

## 项目配置

可以在最近的 `pyproject.toml` 中保存共享参数：

```toml
[tool.envsleuth]
path = "src"
env = [".env.development", ".env.production"]
envignore = ".envignore"
fail-on = ["missing", "dynamic"]
exclude = ["vendor", "generated"]
extensions = [".pyi"]
```

配置中的路径相对于 `pyproject.toml`。为安全起见，自动发现的配置路径
必须留在该文件所在目录内；显式使用 `--config FILE` 时可以主动选择外部
路径。CLI 中显式传入的路径相对于当前目录并具有更高优先级。
`--no-config` 禁用发现。未知键和错误类型会直接报错，避免静默忽略拼写错误。

`--no-fail-on` 只清除配置中的 `fail-on` 列表；如果还要覆盖
`strict = true`，请同时使用 `--no-strict`。

## CLI 参考

### `envsleuth scan`

| 参数 | 说明 |
| --- | --- |
| `--path`, `-p` | 要扫描的目录或文件。默认：配置根目录或 `.` |
| `--env` | env 文件；可重复传入以独立检查多个配置 |
| `--envignore` | `.envignore` 路径。默认：如果存在则使用 `./.envignore` |
| `--strict`, `--no-strict` | 启用或禁用 missing 失败策略 |
| `--fail-on CATEGORY` | 对 `missing`、`extra` 或 `dynamic` 失败；可重复 |
| `--no-fail-on` | 清除配置中的 `fail-on` 列表 |
| `--output`, `-o` | `text`、`json`、`github` 或 `sarif` |
| `--json` | `--output json` 的别名（为了向后兼容而保留） |
| `--no-color` | 禁用 ANSI 颜色（也遵循 `NO_COLOR` 环境变量） |
| `--exclude DIR` | 额外要跳过的目录名。可多次使用 |
| `--ext .EXT` | 额外要扫描的文件扩展名（例如 `.pyi`）。可多次使用 |
| `--verbose`, `-v` | 显示每个变量的所有使用位置 |
| `--no-update-check` | 跳过每周一次的 PyPI 版本检查 |
| `--config FILE`, `--no-config` | 选择或禁用 TOML 配置 |

### `envsleuth generate`

| 参数 | 说明 |
| --- | --- |
| `--path`, `-p` | 要扫描的目录或文件。默认：配置路径/根目录或 `.` |
| `--output`, `-o` | 输出位置。默认：`./.env.example` |
| `--force`, `-f` | 覆盖现有输出文件 |
| `--no-color` | 禁用成功消息中的 ANSI 颜色 |
| `--exclude`, `--ext` | 与 `scan` 中的含义相同 |
| `--no-update-check` | 跳过每周一次的 PyPI 版本检查 |
| `--config FILE`, `--no-config` | 选择或禁用项目配置 |

### 退出状态码

- `0`——命令成功完成。
- `1`——发现了由 `--strict` 或 `--fail-on` 选择的类别。
- `2`——运行错误，例如 `.env` 不存在、扫描不完整、路径无效，或发生
  读取、写入、生成错误。JSON、GitHub 和 SARIF 会尽可能返回一个有效的结构化文档。

## 更新通知

envsleuth 每周最多检查一次 PyPI 上是否有新版本。当新版本可用时，会向 stderr 输出一行提示：

```
ℹ  envsleuth 1.0.1 is available (you have 1.0.0). Run: python -m pip install --upgrade envsleuth
```

检查结果会被缓存，请求使用很短的超时时间，并且在任何错误情况下（离线、网络被屏蔽等）都保持静默。完全禁用该检查：

```bash
# 单次命令
envsleuth scan --no-update-check

# 当前 shell 会话全局设置
export ENVSLEUTH_NO_UPDATE_CHECK=1
```

缓存位于 `~/.cache/envsleuth/last_check.json`（或 `$XDG_CACHE_HOME/envsleuth/...`）。

## 与其他工具对比

| | envsleuth | [dotenv-linter](https://github.com/dotenv-linter/dotenv-linter) | [python-decouple](https://github.com/HBNetwork/python-decouple) |
| --- | --- | --- | --- |
| 扫描**代码**中的环境变量用法 | ✅ | ❌ | ❌ |
| 检查 **.env 文件本身** | ❌ | ✅ | ❌ |
| 运行时配置读取与类型转换 | ❌ | ❌ | ✅ |
| 从代码生成 `.env.example` | ✅ | ❌ | ❌ |
| 语言 | Python | Rust | Python |

这些工具解决的问题不同：envsleuth 扫描源代码，dotenv-linter 检查 `.env`
文件，python-decouple 则在运行时读取配置。

## 依赖

- [click](https://click.palletsprojects.com/)——CLI
- [python-dotenv](https://github.com/theskumar/python-dotenv)——解析 `.env`
- [flashbar](https://github.com/k38f/flashbar)——进度条（一个小巧、零依赖的库；envsleuth 在扫描 20 个以上文件时会使用它）
- [packaging](https://packaging.pypa.io/)——更新检查所用的 PEP 440 版本比较
- [tomli](https://github.com/hukkin/tomli)——在所有受支持的 Python 版本上提供一致的 TOML 解析

扫描器本身只使用 Python 标准库（`ast`）；Pydantic、django-environ
和 python-decouple 只被静态识别。

## 许可证

MIT
