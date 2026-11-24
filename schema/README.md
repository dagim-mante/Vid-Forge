# Storyboard schema

`storyboard.schema.json` defines `projects/<name>/storyboard.json`, the contract between every vid-forge stage. Keyframes, shots, music, narration and assembly each read this file and write their own outputs under `outputs/`. Nothing derived (audio lengths, hashes, timings) is written back into it.

The schema is JSON Schema draft 2020-12.

## How a storyboard is created

```
vid-forge init name=my_film          # creates projects/my_film/input/brief.md
# fill in the brief
vid-forge storyboard project=my_film
vid-forge validate project=my_film   # re-check after hand edits
```

`storyboard` reads `input/brief.md`, asks Gemini for JSON, fills the fields the pipeline owns, and validates the result. `storyboard.json` is written only if validation passes. If it fails, the errors go back to the model for a repair attempt (three attempts in total). Raw attempts are kept in `outputs/storyboard/attempt_N.json` and every model call is logged to `runs.jsonl`.

An existing `storyboard.json` is never overwritten unless you pass `--force`, and then the old file is saved as `storyboard.json.bak`. This protects hand edits.

## Two layers of validation

1. **Schema** (`storyboard.schema.json`): structure, types, ranges, id formats, and the rule that an `i2v` shot has a motion prompt while a `kenburns` shot has a camera move.
2. **Semantic checks** (`pipeline/validate_storyboard.py`): rules a schema cannot express.

| Check | Level |
|---|---|
| Ids are unique within shots, narration lines and music cues | error |
| Every `narration_ref` points to a real narration line | error |
| Every narration line is used by at least one shot | error |
| Every character in `shot.characters` exists in `style.characters` | error |
| `keyframe.selected` does not exceed `keyframe.candidates` | error |
| Shots plus end card total 60 to 90 seconds | error |
| Music cues do not overlap, end after they start, and stay inside the film | error |
| Narration lines appear in order | warning |
| More than 3 English words per second in a shot | warning |
| `i2v` clip longer than 6 seconds | warning |
| Gaps in the music, or music that ends early | warning |

Every pipeline stage should call `validate_file()` at the start of a run and stop on errors.

## Fields

| Field | Purpose |
|---|---|
| `schema_version` | Currently `"1.0"`. Bump it when a change breaks old files. |
| `meta` | Title, audience, intent, narration language (BCP-47), subtitle language (always `en`), fps, both aspect ratios. |
| `meta.generation` | Model, timestamp and SHA-256 of the brief used. Lets later stages notice that the brief changed. |
| `style` | `prefix` prepended to every keyframe prompt, optional `negative` and `palette`, and `characters` (id to description). |
| `narration[]` | `line_NN` with `text_en` (source of truth, used for subtitles) and `text` in the narration language. |
| `shots[]` | `shot_NN` with purpose, duration, narration line, characters, keyframe, motion and crop. |
| `shots[].keyframe` | Scene prompt, seed, number of candidates, and `selected` (1-based, `null` until a human chooses). |
| `shots[].motion` | `i2v` (prompt + seed) or `kenburns` (camera move). |
| `shots[].crop_x` | Where the 9:16 crop sits inside the 16:9 frame, 0 to 1. |
| `music` | `instrumental`, `bpm`, and `cues[]` with time range, mood, intensity and a prompt for the music model. |
| `end_card` | Text (must contain "AI"), optional credits, duration. |

## Fields the pipeline fills in

The model is told to omit these and the `storyboard` command adds them: `schema_version`, `meta.generation`, `keyframe.seed`, `keyframe.candidates` (3), `keyframe.selected` (`null`), `motion.seed` (i2v only) and `crop_x` (0.5). Seeds are derived from the project name and shot id, so regenerating a storyboard keeps them stable.

## Editing by hand

- Keep ids stable. Stages key their cache and output paths on `shot_NN` and `line_NN`, so renumbering invalidates their outputs.
- Set `keyframe.selected` after you review the contact sheet.
- Run `vid-forge validate project=<name>` after every edit.

## Changing the schema

- Adding an optional field is backward compatible.
- Adding a required field, removing a field or tightening a rule is not: bump `schema_version` and update the generator prompt in `pipeline/storyboard.py`.
- Keep the schema and the prompt in step. The prompt embeds the schema, so the model sees every change automatically, but the rules written in prose (shot count, duration, ids) live in the prompt text.