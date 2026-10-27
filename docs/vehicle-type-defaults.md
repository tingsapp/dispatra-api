# Standard Vehicle Type outside defaults

New companies receive representative outside dimensions and conservative total loaded gross weights for all nine active standard Vehicle Types. The retired Box Truck preset carries the same outside profile as Box Truck on initial creation; existing retired records are preserved. These are estimated starting configurations, not measurements of actual fleet vehicles. Verify the actual configuration before operational truck routing. Optional axle weights remain blank because no actual axle layout/load evidence was supplied.

Cargo capacity, surcharges and outside profiles are independent. Seed reruns preserve existing values. Existing profiles are never replaced with these defaults. Trailer entries include the assumed tractor and trailer combination. Widths represent the body/load envelope, excluding mirrors. Flatbed heights include the assumed loaded deck envelope; taller loads require a different configuration. Gross weights use the selected representative GVWR/GCWR as a conservative ceiling, not a claim about actual scale weights or a universal Canadian legal limit.

| Vehicle type | Length (in) | Width (in) | Height (in) | Loaded gross (lb, approx.) |
|---|---:|---:|---:|---:|
| Dry Van Trailer | 900.0 | 102.0 | 162.0 | 80,000 |
| Flatbed Trailer | 900.0 | 102.0 | 162.0 | 80,000 |
| Flatbed Truck | 432.0 | 102.0 | 165.4 | 26,000 |
| Refrigerated Trailer | 900.0 | 102.0 | 162.0 | 80,000 |
| Refrigerated Truck | 324.0 | 96.0 | 144.0 | 17,500 |
| Refrigerated Van | 264.0 | 84.0 | 120.0 | 11,000 |
| Cargo Van | 264.0 | 84.0 | 111.0 | 11,000 |
| Cube Van | 300.0 | 96.0 | 126.0 | 14,500 |
| Box Truck | 432.0 | 102.0 | 165.4 | 26,000 |

Stored gross kg values round upward to whole kilograms, so displayed pounds may be slightly higher. Axle weights remain optional and unset.

## Reference specifications and assumptions

Published specifications informed representative choices; the sources do not certify the complete saved configurations. Assumed overall lengths, reefer equipment allowance and flatbed loaded height are estimates.

- [Ford 2026 Transit technical specifications](https://www.fromtheroad.ford.com/content/dam/fordmediasite/us/en/library/2026/specs/2026_Ford_Transit_Specs.pdf): extended high-roof van envelope and heavy-duty GVWR range.
- [Penske Canada light and medium duty trucks](https://www.pensketruckleasing.com/northamerica/canada/rent-trucks/vehicles/business-light-and-medium-duty-trucks/): cube/straight-truck clearance heights and comparable cargo body classes.
- [Penske commercial fleet specifications](https://www.pensketruckrental.com/commercial-truck-rental/commercial-trucks/view-all-trucks/): representative 17,500 lb reefer, 26,000 lb box/flatbed, trailer width/height classes.
- [Penske tandem sleeper tractor](https://www.pensketruckrental.com/commercial-truck-rental/commercial-trucks/heavy-duty-trucks/tandem-axle-sleeper/): representative 80,000 lb gross combined capacity.

## Per-type assumptions

- **Dry Van Trailer:** 53 ft dry van plus tandem tractor; assumed 75 ft complete combination length, 102 in width, 13 ft 6 in height.
- **Flatbed Trailer:** 53 ft flatbed plus tandem tractor; assumed 75 ft complete combination length and loaded 13 ft 6 in total height including deck/load.
- **Flatbed Truck:** 24 ft deck straight truck; assumed 36 ft overall length and loaded 4.2 m total height, including deck and load.
- **Refrigerated Trailer:** 53 ft reefer plus tandem tractor; assumed 75 ft complete combination length, 102 in width, 13 ft 6 in height.
- **Refrigerated Truck:** 16 ft cabover reefer; assumed 27 ft overall length, 8 ft body width and 12 ft clearance including refrigeration.
- **Refrigerated Van:** Extended high-roof van with assumed additional roof refrigeration clearance; installation-specific estimate.
- **Cargo Van:** Extended high-roof heavy-duty cargo van; Ford Transit published envelope rounded upward.
- **Cube Van:** 16 ft cutaway cube van; assumed 25 ft overall length, 8 ft body width and 10 ft 6 in clearance.
- **Box Truck:** 26 ft straight box truck; assumed 36 ft overall length, 102 in body width and 4.2 m clearance from Penske Canada.

