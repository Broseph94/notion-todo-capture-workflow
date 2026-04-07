# WAL Oppgaver Schema Example

This is a concrete schema snapshot for the Notion task database used in this workspace.

## Database

- Name: `Oppgaver`
- Database ID: `43c652dd-c7bd-46ba-ab60-0da530187ddb`
- Data source URL: `collection://a989beee-7cfb-4ce2-a3f8-d94e1fb4fa36`

## Properties

- `Oppgave` (`title`)
- `Status` (`select`) options: `Ikke startet`, `Pågår`, `Ferdig`
- `Frist` (`date`)
- `Kunde/Prosjekt` (`relation`) -> `collection://29420ba2-39d8-4f55-8400-22f3d45afb59`
- `🏷️ Type` (`multi_select`) options include `📊 Rapportering`, `📣 Annonsering`, `🎨 Kreativt`, `📋 Strategi`, `⚙️ Admin`, `🤝 Møte`
- `🔥 Prioritet` (`select`) options: `🔴 Høy`, `🟡 Medium`, `🟢 Lav`

## Known-Good CLI Mapping

```bash
--database-id "43c652dd-c7bd-46ba-ab60-0da530187ddb" \
--title-property "Oppgave" \
--status-property "Status" \
--due-property "Frist" \
--client-project-property "Kunde/Prosjekt" \
--type-property "🏷️ Type" \
--priority-property "🔥 Prioritet" \
--status-value "Ikke startet"
```
