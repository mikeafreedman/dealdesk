# DealDesk Known Gaps — Image & Chart Pipeline

## Status: PLANNED — Not yet built
## Priority: HIGH — Required for production-ready report

### What's missing
All 14 image/chart slots in the report template currently render as
sage-green placeholder boxes. No image generation code exists anywhere
in the codebase.

### Slots requiring implementation
Maps (3):
  - Fig 3.1  Aerial location map (OpenStreetMap tile compositor)
  - Fig 3.2  Neighborhood context map (Google Maps Static API)
  - Fig 3.3  FEMA flood map (FEMA NFHL API + renderer)

Property photos (2):
  - Fig 2.1  Exterior hero shot (Google Street View Static API)
  - Fig 2.2  Property photo gallery 3x2 grid (OM image extraction)
  - Fig 2.3  Floor plans (OM image extraction)

Charts — financial (4):
  - Fig 12.1 Pro Forma NOI & CFBT bars + DSCR line (matplotlib)
  - Fig 12.2 IRR sensitivity heatmap 7x7 (matplotlib/seaborn)
  - Fig 13.1 Capital stack stacked bar + table (matplotlib)
  - Fig 13.2 Financing options comparison (matplotlib/table)

Charts — market/risk (4):
  - Fig 8.1  Demographic trends 2x2 grid (matplotlib)
  - Fig 9.1  Supply pipeline horizontal bar (matplotlib)
  - Fig 16.1 Risk matrix 4x4 scatter plot (matplotlib)
  - Fig 18.1 Gantt chart LOI to exit (matplotlib/gantt)

### Build approach (when ready)
1. Create map_builder.py — fetches and composites all 3 maps
2. Create chart_builder.py — generates all 8 financial/market charts
3. Update word_builder.py _populate_docx() to call both and insert
   images into the docx replacing placeholder paragraphs
4. Add API keys to config.py and st.secrets:
   GOOGLE_MAPS_API_KEY, OPENSTREETMAP (free/no key needed)
5. Add to requirements.txt: matplotlib, seaborn, Pillow, requests

### Dependencies
- Google Maps Static API key (paid, ~$0.002/request)
- Street View Static API key (paid, ~$0.007/request)  
- FEMA NFHL API (free, no key)
- OpenStreetMap tiles (free, no key, attribution required)
