# Implementation Prompt: Admin Dashboard & Report Generation Time Tracking

This prompt contains step-by-step instructions for an AI agent or a developer to implement the requested features: saving report generation time to Supabase and creating an Admin Dashboard.

## Phase 1: Save Report Generation Time to Supabase

### 1. Database Migration
1. Create a new SQL migration file in the `supabase/migrations/` directory (e.g., `024_report_generation_time.sql`).
2. Add a new column to the `pricing_reports` table to store the time taken to generate the report:
   ```sql
   ALTER TABLE pricing_reports ADD COLUMN generation_time_ms INTEGER;
   ```
3. Apply this migration to your Supabase instance.

### 2. Update Worker to Save Timing Data
1. Open the Python worker script (`worker/main.py`).
2. Locate the place where `total_ms` is calculated (around line 2231: `total_ms = round((time.time() - start_time) * 1000)`).
3. Find the Supabase update call where the `pricing_reports` row is updated with `status = 'ready'`.
4. Include the `generation_time_ms` field in the Supabase update payload.
   ```python
   # Example snippet of the update payload addition:
   "generation_time_ms": total_ms
   ```

---

## Phase 2: Create the Admin UI Dashboard

### 1. Initialize the Admin Project
Since this must be strictly separate from the current website (`src`) and `worker` directories, create a new separate frontend project for the admin dashboard in the project root directory.

Run the following command from the project root:
```bash
npx -y create-vite@latest admin-dashboard --template react-ts
cd admin-dashboard
npm install
npm install @supabase/supabase-js tailwindcss postcss autoprefixer react-router-dom lucide-react
npx tailwindcss init -p
```
*(Configure TailwindCSS according to the standard Vite setup by updating `tailwind.config.js` and `index.css`.)*

### 2. Connect to Supabase
1. Inside the `admin-dashboard` project, create a `.env` file with your Supabase credentials:
   ```env
   VITE_SUPABASE_URL=your_supabase_url
   VITE_SUPABASE_ANON_KEY=your_supabase_anon_key
   ```
2. Create a Supabase client instance (e.g., `src/lib/supabase.ts`):
   ```typescript
   import { createClient } from '@supabase/supabase-js';
   export const supabase = createClient(
     import.meta.env.VITE_SUPABASE_URL,
     import.meta.env.VITE_SUPABASE_ANON_KEY
   );
   ```

### 3. Build the Dashboard UI
Implement a rich, dynamic, and premium-looking UI using modern web design principles (glassmorphism, micro-animations, curated palettes). 

1. **Dashboard Layout**: Create a responsive side navigation bar and a main content area.
2. **Data Fetching**: Fetch data from the `pricing_reports` table. You may need to join with `saved_listings` or just use the `input_address` from `pricing_reports`.
   ```typescript
   // Example query
   const { data, error } = await supabase
     .from('pricing_reports')
     .select(`
       id,
       created_at,
       input_address,
       status,
       generation_time_ms,
       result_summary
     `)
     .order('created_at', { ascending: false });
   ```
3. **Metrics Cards (KPIs)**:
   - **Total Reports Generated**: Count of all reports.
   - **Average Generation Time**: Average of `generation_time_ms` across successful reports.
   - **Active Listings**: Count of unique `input_address`.
4. **Data Table**:
   Display a table listing the recent reports with the following columns:
   - **Listing**: Show the `input_address`.
   - **Comparables Found**: Extract this from the `result_summary` JSON object (e.g., `result_summary.comparables.length` or `result_summary.total_comparables`).
   - **Generation Time**: Convert `generation_time_ms` to a readable format (e.g., `(generation_time_ms / 1000).toFixed(2) + 's'`).
   - **Status**: Visual badge for `queued`, `ready`, or `error`.
   - **Created At**: Timestamp of the report.

### 4. Design Aesthetics
Ensure the UI looks premium to WOW the user:
- **Typography**: Use a modern font like `Inter`, `Roboto`, or `Outfit` from Google Fonts.
- **Colors**: Use a dark mode theme with highly customized HSL color palettes (avoid plain generic colors).
- **Animations**: Apply smooth hover effects on table rows, button transitions, and loading states.
- **Components**: Ensure all components use predefined design tokens. Do not use plain tables; utilize rounded corners, borders, and subtle shadows.
- **Quality**: Avoid creating a simple MVP. Aim for a state-of-the-art visual presentation.
