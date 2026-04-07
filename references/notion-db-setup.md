# Notion Database Setup

Use this setup for CLI fallback mode (`scripts/capture_to_notion.py`) when the Notion connector is not available.
In chat sessions where Notion connector tools are available, prefer direct connector writes and skip local token setup.

## Required

- Integration token with access to the target database (`NOTION_API_TOKEN`).
- Database ID (`NOTION_DATABASE_ID`), found in the database URL.
- Database shared with the integration.

## Recommended properties

Default CLI flags expect these names:

- `Name` (type: `title`)
- `Status` (type: `status` or `select`)
- `Due` (type: `date`)
- `Source` (type: `rich_text`, optional)

If property names differ, pass explicit flags:

```bash
--title-property "Task" --status-property "State" --due-property "Deadline"
```

## Environment variables

```bash
export NOTION_API_TOKEN="secret_xxx"
export NOTION_DATABASE_ID="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

## Troubleshooting

- `403`: The integration does not have access to the database. Share the database with the integration.
- `404`: Wrong database ID or wrong workspace.
- `Validation failed`: One or more property names do not match database schema.
