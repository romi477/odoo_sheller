These rules are for the Odoo major this session runs. They cover the code you
run in os_exec and the module code you write while doing it, Python and XML
alike. Write for this major, not for the one you remember best: a spelling
that is right on 17 is an error on 18, and both look plausible.

A name that is not in these rules is not a fact. Check it in the instance
before you rely on it: os_source(model=..., method=...) for a method, a file
path for a view or a manifest. The running source wins over these rules and
over memory alike.

## On every major from 15 to 20

- `@api.multi` and `@api.one` are gone. A method on `self` handles any number
  of records; open with `self.ensure_one()` when it handles exactly one.
- Core `create` is `@api.model_create_multi` and takes `vals_list`. Calling
  it with one dict still works. An override takes the list form.
- x2many values go through `Command` (`from odoo.fields import Command`):
  `Command.create(vals)`, `Command.link(id)`, `Command.set(ids)`,
  `Command.clear()`. A raw `(0, 0, vals)` tuple still works; match the
  module you are editing.
- Assets are declared in the manifest's `assets` key, never in an XML
  template that inherits `web.assets_backend`.
- Prefer `env.ref('module.xml_id')` over a search by name for a record that
  has an XML ID.
- `sudo()` drops access checks for the code after it. In the shell you are
  already superuser; module code you write is not, so test it as a real
  user with `with_user(user)` before you call it done.
