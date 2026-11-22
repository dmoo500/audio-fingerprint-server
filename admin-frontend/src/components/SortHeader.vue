<script setup lang="ts">
defineProps<{ label: string; direction?: "asc" | "desc" }>();
defineEmits<{ sort: [] }>();
</script>

<template>
  <th
    scope="col"
    :aria-sort="
      direction === 'asc'
        ? 'ascending'
        : direction === 'desc'
          ? 'descending'
          : undefined
    "
  >
    <button class="sort" type="button" @click="$emit('sort')">
      {{ label }}
      <svg
        class="sort-indicator"
        :data-direction="direction"
        width="10"
        height="16"
        viewBox="0 0 10 16"
        aria-hidden="true"
      >
        <path d="M5 1 9 7H1Z" />
        <path d="M1 9h8l-4 6Z" />
      </svg>
    </button>
  </th>
</template>
