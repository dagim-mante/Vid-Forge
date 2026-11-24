# Storyboard schema

```
schema/                      this folder: the schema and its documentation
pipeline/script/             the script stage
    storyboard.py            brief + reference images -> production/storyboard.json
    validate_storyboard.py   schema + semantic checks; every stage should call it
    reference_images.py      scans and checks input/references/
projects/<name>/             one folder per film
    input/brief.md
    input/references/        optional reference images
    production/
        storyboard.json      the generated contract (and storyboard.json.bak after --force)
    outputs/storyboard/      raw model attempts
    runs.jsonl               one line per model call
```

`storyboard.schema.json` defines `projects/<name>/storyboard.json`, the contract between every vid-forge stage. Keyframes, shots, music, narration and assembly each read this file and write their own outputs under `outputs/`. Nothing derived (audio lengths, hashes, timings) is written back into it.

The schema is JSON Schema draft 2020-12.

## How a storyboard is created

```
vid-forge init name=my_film          # creates projects/my_film/input/brief.md
# fill in the brief
vid-forge storyboard project=my_film
vid-forge validate project=my_film   # re-check after hand edits
```

`storyboard` reads `input/brief.md`, asks Gemini for JSON, fills the fields the pipeline owns, and validates the result. `production/storyboard.json` is written only if validation passes. If it fails, the errors go back to the model for a repair attempt (three attempts in total). Raw attempts are kept in `outputs/storyboard/attempt_N.json` and every model call is logged to `runs.jsonl`.

An existing `production/storyboard.json` is never overwritten unless you pass `--force`, and then the old file is saved next to it as `storyboard.json.bak`. This protects hand edits.

## Reference images

Drop images into `projects/<name>/input/references/`. The filename is the metadata:

| Filename | Kind | Meaning |
|---|---|---|
| `character_<name>.png` | character | Identity of a recurring character. `<name>` must be a key in `style.characters`. |
| `scene_<name>.jpg` | scene | A place or an object. Listed in `shot.references` for shots set there. |
| `style_<name>.webp` | style | The overall look. Applies to every shot, so it is never listed per shot. |

`<name>` is lowercase letters, digits and underscores, starts with a letter, and is 2 to 32 characters. Formats are png, jpg and webp, up to 4 MB each, 8 images and 12 MB in total (they are sent inline to the model). The real format is checked from the file's first bytes, not just the extension.

What `vid-forge storyboard` does with them:

1. Scans and checks the folder, and stops with a list of problems if any image is unusable.
2. Sends every image to Gemini with its id, so descriptions in the brief and the storyboard match what the images show.
3. Builds the top-level `references` registry itself: `id`, `kind`, `path` and `sha256` come from the files on disk. Only `description` comes from the model, so a hash or path can never be invented.
4. Adds each character reference to every shot that lists that character in `shot.characters`.

Every reference keeps a text `description`. A keyframe model that cannot take image input can still use it, so the schema does not tie you to one model. Later stages should include each reference's `sha256` in their cache key, so changing an image regenerates only the shots that use it.

Reference image paths in the storyboard (for example `input/references/character_girl.png`) are relative to the project folder, not to `production/`. If you hand-edit `production/storyboard.json` or replace an image, `vid-forge validate` tells you when a file is missing (error) or no longer matches its recorded hash (warning).

## Two layers of validation

1. **Schema** (`storyboard.schema.json`): structure, types, ranges, id formats, and the rule that an `i2v` shot has a motion prompt while a `kenburns` shot has a camera move.
2. **Semantic checks** (`pipeline/script/validate_storyboard.py`): rules a schema cannot express.

| Check | Level |
|---|---|
| Ids are unique within shots, narration lines and music cues | error |
| Every `narration_ref` points to a real narration line | error |
| Every narration line is used by at least one shot | error |
| Every character in `shot.characters` exists in `style.characters` | error |
| Reference ids are unique, the kind matches the id prefix, the file name matches the id | error |
| Every `shot.references` id exists, and none is a style reference | error |
| Every character reference has a matching key in `style.characters` | error |
| Every reference file exists on disk | error |
| A reference file no longer matches its recorded `sha256` | warning |
| A character in a shot has a reference image the shot does not list | warning |
| A character or scene reference is not used by any shot | warning |
| More than one style reference | warning |
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
| `references[]` | Reference image registry: `id`, `kind`, `path`, text `description`, `sha256`. Optional. |
| `shots[].references` | Character and scene reference ids used for the shot (at most 4). |
| `shots[].keyframe` | Scene prompt, seed, number of candidates, and `selected` (1-based, `null` until a human chooses). |
| `shots[].motion` | `i2v` (prompt + seed) or `kenburns` (camera move). |
| `shots[].crop_x` | Where the 9:16 crop sits inside the 16:9 frame, 0 to 1. |
| `music` | `instrumental`, `bpm`, and `cues[]` with time range, mood, intensity and a prompt for the music model. |
| `end_card` | Text (must contain "AI"), optional credits, duration. |

## Fields the pipeline fills in

The model is told to omit these and the `storyboard` command adds them: `schema_version`, `meta.generation`, `keyframe.seed`, `keyframe.candidates` (3), `keyframe.selected` (`null`), `motion.seed` (i2v only) and `crop_x` (0.5), plus `references[].kind`, `path` and `sha256`, which come from the files on disk. Seeds are derived from the project name and shot id, so regenerating a storyboard keeps them stable.

## Editing by hand

- Keep ids stable. Stages key their cache and output paths on `shot_NN` and `line_NN`, so renumbering invalidates their outputs.
- Set `keyframe.selected` after you review the contact sheet.
- Run `vid-forge validate project=<name>` after every edit.

## Changing the schema

- Adding an optional field is backward compatible (this is how `references` was added without a version bump).
- Adding a required field, removing a field or tightening a rule is not: bump `schema_version` and update the generator prompt in `pipeline/script/storyboard.py`.
- Keep the schema and the prompt in step. The prompt embeds the schema, so the model sees every change automatically, but the rules written in prose (shot count, duration, ids) live in the prompt text.