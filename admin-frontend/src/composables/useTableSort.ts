import { ref } from "vue";

export function useTableSort(initial: string) {
  const field = ref(initial);
  const direction = ref<"asc" | "desc">("asc");

  function sort(next: string) {
    direction.value =
      field.value === next && direction.value === "asc" ? "desc" : "asc";
    field.value = next;
  }

  function compare(a: string | number, b: string | number) {
    const result =
      typeof a === "number" && typeof b === "number"
        ? a - b
        : String(a).localeCompare(String(b), undefined, {
            numeric: true,
            sensitivity: "base",
          });
    return direction.value === "asc" ? result : -result;
  }

  return { field, direction, sort, compare };
}
