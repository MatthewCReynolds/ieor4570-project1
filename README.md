# Domestique

IEOR 4570, Project 1: a tool-calling cycling ride-planner agent built on the class
`gemini-web-tool-calling` starter (FastAPI + LiteLLM + Gemini), deployed on Cloud Run.

Team: mcr2235, rw3024

#1. Domestique

#2. Description
Domestique, from the french for a member of a cycling team who's role is to support, is a chatbot agent equiped with several tools to assist in various cycling related questions. Domestique helps to plan rotues, check weather, find refuelling stations, and many other helpful tasks. This is designed to be used by both recreational and competitive cyclists alike to help them enjoy their rides more.

#3. Tool Description
plan_route — routes two places by bike with BRouter and returns distance, climbing, max grade, paved share, an elevation profile and a GPX download.
get_ride_forecast — hourly temperature, rain chance, wind and daylight for a place and day, each hour scored 0–100 for riding, plus the best window.
analyze_headwind (original) — crosses the route's bearing, mile by mile, with the wind forecast at the time you'll be there: headwind cost in minutes, worst stretch, and whether to ride it in reverse.
find_refuel_stops — water fountains, bike shops, cafes and restrooms within a corridor of the route, tagged with their mile marker, from OpenStreetMap.
recommend_tire_pressure (original) — front and rear psi from rider and bike weight, tire width, surface, tubeless and hookless rims, using Berto's 15% tire-drop data.
estimate_ride_effort — moving time, average speed, kilojoules and a fueling plan from a physics power model on the real grade profile.
assess_route_safety (original) — a Level-of-Traffic-Stress comfort score built from the road class, speed limit and bike infrastructure of every segment, with the worst stretches flagged.
analyze_sun_exposure (original) — where the sun sits relative to your heading at each point of the ride: glare stretches, lights needed, latest start to finish by sunset, and UV.

#4. Explanation

Example queries:
###Q1. Plan a road ride from Columbia University, New York to Nyack, NY and tell me how long it takes at 170 W.
###Response:
(There will be a visual, and then this text response)
That 24-mile road route from Columbia University to Nyack covers 1,041 feet of climbing and will take you 1 hour and 17 minutes at 170 W (assuming a 165 lb rider and 20 lb bike).

Expect an average speed of 18.6 mph. The route is entirely paved, with about 51% busier roads and 42% quieter roads. Have a great ride up to Nyack!

###Q2. Which direction should I ride a loop from Boulder, CO via Lyons, CO on Saturday at 9am to avoid the headwind?
###Response:
(there will be visuals, and this response)
Ride the loop as planned (counter-clockwise, heading north toward Lyons first).

Saturday morning's winds out of the west at around 6 mph (gusts to 15 mph) are light enough that direction won't make or break your ride. The route analysis shows a roughly neutral wind with about half tailwind exposure overall, so sticking to the standard route keeps you efficient without needing to reverse the loop.

(Assumptions: 165 lb rider, 20 lb bike, 150 W output).

###Q3. Is the Columbia to Nyack ride safe for a nervous rider, and will I be riding into the sun if I leave at 5pm?
###Response:
(there will be visuals)
No, this route is not a good match for a nervous rider, and yes, you will be riding straight into a low-sun glare if you leave at 5:00 PM.

(Assumptions: 165 lb rider, 20 lb bike, 150 W steady power).
Safety & Comfort

Despite choosing the safest routing style, this route scores a 33/100 (high stress) for a cautious rider. Over half the ride (52%) is spent on high-stress mixed-traffic roads, and you'll encounter nearly 3 traffic signals per mile. If you are nervous about traffic, this stretch along the Hudson corridor will feel intense.
Sun & Daylight

If you depart at 5:00 PM (using a fitness pace of 150 W):

    Sunset is at 6:28 PM, meaning you will finish right at dusk (around 6:24 PM).
    The Sun: Because October sunsets sit low in the west/southwest, you will face direct, blinding glare right in your eyes early in the ride (around miles 4.1 to 4.6).
    The Verdict: 5:00 PM is the absolute latest safe departure to beat total darkness today, but the combination of heavy traffic stress and late-afternoon glare makes this a tough slot for a nervous rider. If you want to tackle this route, aim for a morning or early afternoon start instead.


