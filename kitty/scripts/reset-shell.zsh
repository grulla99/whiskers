#!/bin/zsh

# Clear any leaked kitty keyboard enhancement state before the interactive
# shell starts in a newly created tab/window.
printf '\e[<999u'

exec /bin/zsh -il
