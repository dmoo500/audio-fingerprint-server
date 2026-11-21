<script setup lang="ts">
export type Chart = {
  maximum: number;
  secondary_maximum?: number | null;
  start: string;
  end: string;
  last_at: string;
  series: {
    label: string;
    color: string;
    path: string;
    dots: [number, number][];
    last: number;
    axis?: "left" | "right";
  }[];
};
defineProps<{ charts: [string, Chart | null][] }>();
</script>

<template>
  <div
    v-if="charts.some(([, chart]) => chart?.series?.length)"
    class="grid charts"
  >
    <section v-for="[title, chart] in charts" :key="title" class="card">
      <template v-if="chart?.series?.length">
        <h3>{{ title }}</h3>
        <div class="chart-area">
          <div class="chart-axis">
            <span>{{ chart.maximum.toLocaleString() }}</span
            ><span>{{ (0).toLocaleString() }}</span>
          </div>
          <svg
            width="100%"
            height="130"
            viewBox="-5 -5 610 110"
            preserveAspectRatio="none"
            role="img"
            :aria-label="`${title} over time`"
          >
            <path
              d="M0,0 H600 M0,50 H600 M0,100 H600"
              fill="none"
              stroke="currentColor"
              opacity=".2"
              vector-effect="non-scaling-stroke"
            />
            <g v-for="series in chart.series" :key="series.label">
              <path
                :d="series.path"
                fill="none"
                :stroke="series.color"
                stroke-width="2"
                vector-effect="non-scaling-stroke"
              />
              <circle
                v-for="([x, y], index) in series.dots"
                :key="index"
                :cx="x"
                :cy="y"
                r="2"
                :fill="series.color"
              />
            </g>
          </svg>
          <div
            v-if="chart.secondary_maximum != null"
            class="chart-axis"
            aria-label="Cached covers scale"
          >
            <span>{{ chart.secondary_maximum.toLocaleString() }}</span>
            <span>0</span>
          </div>
        </div>
        <div class="chart-dates">
          <span>{{ chart.start }}</span
          ><span v-if="chart.start !== chart.end">{{ chart.end }}</span>
        </div>
        <p>Last measured {{ chart.last_at }} UTC</p>
        <ul class="legend">
          <li v-for="series in chart.series" :key="series.label">
            <span :style="{ color: series.color }">●</span> {{ series.label }}:
            <strong>{{ series.last.toLocaleString() }}</strong>
            <small v-if="series.axis === 'right'"> (right axis)</small>
          </li>
        </ul>
      </template>
      <p v-else>No hourly measurements for {{ title }} yet.</p>
    </section>
  </div>
  <p v-else class="card">
    No hourly measurements yet. History starts when the worker saves its first
    sample.
  </p>
</template>
