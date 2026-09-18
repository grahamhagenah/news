# Bay Area venues to consider

What Pushpin Bay Area reads now, and what could be added. Each line says how its listings would be read: a
reader this build already has, or one that would have to be written. Checked 18 September 2026; Bimbo's added the same day.

## Reading now

| Venue | Where | Kind | How |
| --- | --- | --- | --- |
| Roxie | San Francisco (Mission) | Film | `roxie` — its calendar, a table for each month |
| Alamo Drafthouse New Mission | San Francisco (Mission) | Film | `alamo` — the SF market's schedule, narrowed to New Mission |
| The Independent | San Francisco (Divisadero) | Music | `ticketweb` — its TicketWeb listing |
| Bimbo's 365 Club | San Francisco (North Beach) | Music | `ticketweb` — the same listing, in a third template |
| Oakland Museum of California | Oakland | Art | `tribe` — its WordPress calendar's API |

## Berkeley, which the pages now say this covers

Nothing in Berkeley reads yet. BAMPFA (film and exhibitions both), the UC Theatre, Freight & Salvage and
Berkeley Rep are the ones worth having, and each needs work: BAMPFA keeps its own calendar, Freight &
Salvage and SFJAZZ turn a plain reader away (403), and the UC Theatre and Berkeley Rep publish nothing
structured. Until one of them lands, the Bay Area's listings are San Francisco and Oakland only.

## Ready to add, with a reader this build already has

| Venue | Where | Kind | How | Note |
| --- | --- | --- | --- | --- |
| Alamo Drafthouse (Valley Fair, Mountain View) | South Bay | Film | `alamo` | Already in the SF market; drop the `#new-mission` to take the lot, or name another theater |

## Wants a reader written, in rough order of how much it would take

| Venue | Where | Kind | What it publishes |
| --- | --- | --- | --- |
| Rickshaw Stop, Great American Music Hall | San Francisco | Music | See Tickets — one reader would cover both |
| Starline Social Club, Yoshi's | Oakland | Music | Etix — likewise, one reader for both |
| The New Parish | Oakland | Music | Its homepage carries no listing to read; tickets are elsewhere |
| The Chapel | San Francisco (Valencia) | Music | Tixr and See Tickets |
| BAMPFA | Berkeley | Film, art | Its own calendar; films and exhibitions both, and Eventbrite for some |
| The UC Theatre | Berkeley | Music | Nothing structured on its site |
| Freight & Salvage | Berkeley | Music | Turns a plain reader away (403) |
| SFMOMA | San Francisco | Art | Its own events pages |
| Exploratorium | San Francisco (After Dark) | Art | Its own calendar |
| Grand Lake Theatre | Oakland | Film | A plain page, hand-kept; no feed found |
| Fine Arts Museums (de Young, Legion of Honor) | San Francisco | Art | Blocks a plain reader (403); would need asking them |

## Worth a look, not yet checked

The Fillmore and Bimbo's 365 Club (San Francisco), The UC Theatre and Freight & Salvage (Berkeley), The Stork
Club and Starline Social Club (Oakland), Fox Theater and Paramount Theatre (Oakland), the Castro Theatre
(reopened as a music hall), the Balboa and the Vogue (San Francisco Neighborhood Theater Foundation), the 4-Star,
the Stanford Theatre (Palo Alto), Berkeley Art Center, the Lab, Gray Area, and the SF Jazz Center.

Adding a venue is a line in `events/sources-bayarea.txt`, its address in `VENUE_ADDRESSES`, and a reader if
none of the ones listed at the top of `events/sources.txt` fits.
