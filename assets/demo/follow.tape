# The scripted demo. Run it through scripts/record_demo.sh, never directly:
# that script builds the isolated world (HOME, WORK, a private tmux server)
# this tape's launch.sh expects in the current directory, and tears it down.
#
# vhs 0.12.0's own ffmpeg step silently writes no GIF against ffmpeg 9.x
# (measured 2026-09-23), so the tape only captures frames; the GIF and MP4
# are assembled by scripts/render_demo.sh. Output must stay a RELATIVE path.
Output frames/

Set Shell bash
Set Theme "GruvboxDark"
Set FontSize 16
Set Width 1400
Set Height 700
Set Padding 12
Set TypingSpeed 0

Hide
Type "bash launch.sh"
Enter
Sleep 1s
Show

Wait+Screen@180s /end of demo/
Sleep 3s
