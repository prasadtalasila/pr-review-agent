## Code-smell baseline

On top of whatever the repository writes down, every review carries a fixed
baseline: twelve of the code smells in Martin Fowler's *Refactoring*
(chapter 3). It applies even where a repository documents nothing. Three
rules bind it:

- **The repository overrides it.** A rule in the review standards always
  wins. Where one endorses something the baseline would flag, the smell is
  not a finding.
- **A smell is a judgement call, never a violation.** Report it as `minor`
  or `nit`, never higher. Name the smell in the body; the title still states
  a consequence, as for any finding.
- **The diff has to introduce it or make it worse.** The causation rule
  applies unchanged: a smell that was in the file before this change is a
  pre-existing problem. Skip anything a linter or formatter already
  enforces.

Each entry says what the smell is, then the usual remedy.

- **Mysterious Name** -- a function, variable or type whose name does not
  say what it does or holds. Rename it; if no honest name comes, the design
  is unclear.
- **Duplicated Code** -- the same logic shape in more than one hunk or file
  of the change. Extract the shared shape and call it from both.
- **Feature Envy** -- a function that uses another object's data more than
  its own. Move it next to the data it uses.
- **Data Clumps** -- the same few fields or parameters travelling together
  again and again. Bundle them into one type and pass that.
- **Primitive Obsession** -- a primitive or a string standing in for a
  domain concept. Give the concept a small type of its own.
- **Repeated Switches** -- the same `switch` or `if` cascade over the same
  value in more than one place. Replace it with polymorphism, or with one
  mapping both places share.
- **Shotgun Surgery** -- one logical change forcing scattered edits across
  many files. Gather what changes together into one module.
- **Divergent Change** -- one module edited for several unrelated reasons.
  Split it so each part changes for one reason.
- **Speculative Generality** -- abstractions, parameters or hooks for a need
  nothing in the change has. Delete them; inline until a real need appears.
- **Message Chains** -- long `a.b().c().d()` navigation the caller should not
  depend on. Hide the walk behind one method on the first object.
- **Middle Man** -- a class or function that mostly delegates onward. Remove
  it and call the real target directly.
- **Refused Bequest** -- a subclass or implementation that ignores or
  overrides most of what it inherits. Replace the inheritance with
  composition.
