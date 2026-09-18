# Bay Area venues to consider

What Pushpin Bay Area reads now, and what could be added. Each line says how its listings would be read: a
reader this build already has, or one that would have to be written. Checked 18 September 2026; Bimbo's, BAMPFA, Rickshaw Stop, the Great American Music Hall, Yoshi's, The
Chapel, SFMOMA and the Exploratorium added the same day.

## Reading now

| Venue | Where | Kind | How |
| --- | --- | --- | --- |
| Roxie | San Francisco (Mission) | Film | `roxie` — its calendar, a table for each month |
| Alamo New Mission | San Francisco (Mission) | Film | `alamo` — the SF market's schedule, narrowed to New Mission |
| The Independent | San Francisco (Divisadero) | Music | `ticketweb` — its TicketWeb listing |
| Bimbo's 365 Club | San Francisco (North Beach) | Music | `ticketweb` — the same listing, in a third template |
| Rickshaw Stop | San Francisco (Hayes Valley) | Music | `seetickets` — See Tickets' listing in its own site |
| Great American Music Hall | San Francisco (Tenderloin) | Music | `seetickets` — the same |
| Yoshi's | Oakland (Jack London Square) | Music | `yoshis` — its upcoming events, with the day in its links' labels |
| The Chapel | San Francisco (Valencia) | Music | `seetickets` — the same listing again, no new reader |
| SFMOMA | San Francisco (SoMa) | Art, film | `sfmoma` — the data behind its events page; a screening goes with the films |
| Exploratorium | San Francisco (Pier 15) | Art | `exploratorium` — its calendar, After Dark and the rest |
| Oakland Museum of California | Oakland | Art | `tribe` — its WordPress calendar's API |
| BAMPFA | Berkeley | Film, art | `bampfa` — its calendar, a month at a time; its labels sort films from talks |

## Berkeley

BAMPFA reads, which is Berkeley's films and its talks both. The UC Theatre, Freight & Salvage and Berkeley Rep
are the ones left worth having: Freight & Salvage turns a plain reader away (403), and the other two publish
nothing structured.

## Ready to add, with a reader this build already has

| Venue | Where | Kind | How | Note |
| --- | --- | --- | --- | --- |
| Alamo Drafthouse (Valley Fair, Mountain View) | South Bay | Film | `alamo` | Already in the SF market; drop the `#new-mission` to take the lot, or name another theater |

## Wants a reader written, in rough order of how much it would take

| Venue | Where | Kind | What it publishes |
| --- | --- | --- | --- |
| Starline Social Club | Oakland | Music | Its listings aren't in the page it serves; a Rockhouse widget loads them |
| The New Parish | Oakland | Music | Its homepage carries no listing to read; tickets are elsewhere |
| The UC Theatre | Berkeley | Music | Nothing structured on its site |
| Freight & Salvage | Berkeley | Music | Turns a plain reader away (403) |
| Grand Lake Theatre | Oakland | Film | A plain page, hand-kept; no feed found |
| Fine Arts Museums (de Young, Legion of Honor) | San Francisco | Art | Blocks a plain reader (403); would need asking them |

## Worth a look, not yet checked

The Fillmore and Bimbo's 365 Club (San Francisco), The UC Theatre and Freight & Salvage (Berkeley), The Stork
Club and Starline Social Club (Oakland), Fox Theater and Paramount Theatre (Oakland), the Castro Theatre
(reopened as a music hall), the Balboa and the Vogue (San Francisco Neighborhood Theater Foundation), the 4-Star,
the Stanford Theatre (Palo Alto), Berkeley Art Center, the Lab, Gray Area, and the SF Jazz Center.

Adding a venue is a line in `events/sources-bayarea.txt`, its address in `VENUE_ADDRESSES`, and a reader if
none of the ones listed at the top of `events/sources.txt` fits.
