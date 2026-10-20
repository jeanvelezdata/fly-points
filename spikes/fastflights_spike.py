import sys, json
from fast_flights import create_query, FlightQuery, Passengers, get_flights
q = create_query(flights=[FlightQuery(date=sys.argv[3], from_airport=sys.argv[1], to_airport=sys.argv[2])],
                 seat=sys.argv[4], trip="one-way", passengers=Passengers(adults=1), currency="USD", language="en")
print(q.url())
res = get_flights(q)
print(type(res), len(res))
for f in list(res)[:8]:
    print(repr(f)[:400])
