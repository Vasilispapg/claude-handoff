# Project-memory demo — renders docs/assets/demo-brief.gif.
# Regenerate: stage a demo store as HOME with a `claude` shim on PATH
# (see git history of this file), then: vhs docs/assets/demo-brief.tape
Output docs/assets/demo-brief.gif
Set FontSize 16
Set Width 980
Set Height 770
Set Padding 14
Set TypingSpeed 35ms
Type "chf --brief --llm claude-cli --project shop-api"
Sleep 400ms
Enter
Sleep 1800ms
Type "tail -n +2 ~/.claude/briefs/shop-api.md"
Sleep 400ms
Enter
Sleep 5200ms
