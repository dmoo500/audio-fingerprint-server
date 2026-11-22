export type ListView = {
  search: string;
  filterField: string;
  operator: string;
  filterValue: string;
  appliedFilter: Record<string, unknown> | null;
  order: { field: string; direction: "asc" | "desc" }[];
  skip: number;
  limit: number;
  hiddenFields: string[];
  loadedCount?: number;
  scrollY?: number;
};

export type SubmissionNavigation = {
  dataset: string;
  listPath: string;
  view: ListView;
  query: string;
  offset: number;
  ids: string[];
  total: number;
};
