# AGENTS.md

Инженерные правила и факты проекта Milly backend для coding-агентов.
Продуктовое описание — в `README.md`; в каталоге `backend/` он пока не создан, поэтому не выдумывай продуктовые детали.
Здесь — только конвенции работы с кодом и актуальное состояние проекта.

Обязательный процесс разработки — SOLID+KISS+TDD; докстринги обязательны и поддерживаются в актуальном состоянии.

## 7. Докстринги

Докстринги обязательны для всех новых функций, методов и классов. **Докстринг поддерживается в актуальном состоянии**: при изменении сигнатуры, поведения или состава исключений функции обновляй её докстринг в том же изменении. Устаревший или неверный докстринг — такой же дефект, как падающий тест.

Все функции, методы и классы в `src/` уже имеют докстринги; новые и изменяемые функции документируй сразу по шаблону ниже.

- Язык — английский; заголовки секций — как в шаблоне ниже. Русскоязычные докстринги старого кода приводи к шаблону при доработке соответствующего кода.
- Классы — однострочный докстринг: что представляет класс (сущность, DTO, ORM-таблица, исключение, use case, контракт `Protocol`).
- Секции адаптивные: summary + `Purpose` — всегда; `Args`, `Returns`, `Raises` — когда применимо; `Examples`, `Notes` — где добавляют ценность (не нужны у тривиальных `__init__`, property и Protocol-заглушек).
- `Raises` — только реально поднимаемые или явно пробрасываемые исключения; `Examples` — достоверные примеры, согласованные с кодом. Параметры, исключения и поведение не выдумывать.
- В `Notes` отражай границы транзакций («не коммитит; коммит — на вызывающем слое, запись через `flush()`»), сайд-эффекты, идемпотентность, поля, генерируемые БД.
- Методы `Protocol`/ABC-заглушек описывают контракт, который обязан выполнить реализатор; `Args`/`Returns` для них обязательны.

```python
def calculate_order_total(items: list[dict], discount_percent: float = 0) -> float:
    """
    Calculate the total order cost.

    Purpose:
        Sums item costs and applies the discount; used when generating receipts.

    Args:
        items: Order items, each a dict with "price" (float) and "quantity" (int).
        discount_percent: Discount from 0 to 100. Defaults to 0.

    Returns:
        float: Total cost rounded to 2 decimal places.

    Raises:
        ValueError: If `items` is empty or a price/quantity/percent is out of range.

    Examples:
        >>> calculate_order_total([{"price": 100.0, "quantity": 2}], discount_percent=10)
        180.0

    Notes:
        - Discount is applied before rounding; no side effects.
    """
```

## 8. graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
