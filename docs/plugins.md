# Plugins

A plugin is any subclass of `automation.plugins.base.Plugin` discovered on
import. Plugins live in the `plugins_external/` directory by default
(configurable via `plugins.paths`).

## Skeleton

```python
# plugins_external/my_plugin/__init__.py
from automation.plugins.base import Plugin


class MyPlugin(Plugin):
    name = "my_plugin"
    version = "1.0.0"
    description = "What it does."
    default_enabled = True

    async def on_start(self):
        self.log.info("starting with config=%s", self.config)
        await self.engine.event_bus.subscribe("task.complete", self.on_task_complete)

    async def on_task_complete(self, event):
        self.log.info("task done: %s", event.payload)

    async def on_stop(self):
        pass
```

## Lifecycle hooks

| Hook | When |
|---|---|
| `on_start` | Plugin is enabled and engine starting |
| `on_stop` | Plugin is being disabled or engine stopping |
| `on_reload` | Configuration was reloaded |
| `on_task_begin(event)` | Auto-subscribed to `task.begin` |
| `on_task_complete(event)` | Auto-subscribed to `task.complete` |
| `on_error(event)` | Auto-subscribed to `task.error` |
| `healthcheck()` | Optional: return `bool` |

Failures in any hook are caught by the loader and logged — your plugin
**cannot** crash the engine.

## Configuration

Per-plugin config goes under `plugins.config.<name>` in `config.json`. The
plugin receives it as `self.config`:

```json
{
  "plugins": {
    "enabled": {"my_plugin": true},
    "config": {"my_plugin": {"interval": 30}}
  }
}
```

## Enable / disable at runtime

```bash
automation-cli plugins disable my_plugin
automation-cli plugins enable my_plugin
automation-cli plugins restart my_plugin
```

Config keys (`plugins.enabled.<name>`) override the plugin's
`default_enabled` flag at load time.

## Examples shipped

- `example_heartbeat` — schedules a recurring `heartbeat.tick` event
- `example_metrics` — counts every event flowing through the bus
- `example_task` — submits a sample task on each heartbeat (disabled by default)
