import pluginVue from "eslint-plugin-vue";
import {
  defineConfigWithVueTs,
  vueTsConfigs,
} from "@vue/eslint-config-typescript";
import eslintConfigPrettier from "eslint-config-prettier";
import oxlint from "eslint-plugin-oxlint";

export default defineConfigWithVueTs(
  { ignores: ["**/dist/**", "**/coverage/**"] },
  vueTsConfigs.eslintRecommended,
  pluginVue.configs["flat/recommended"],
  vueTsConfigs.recommended,
  {
    rules: {
      "vue/multi-word-component-names": "off",
      "vue/attribute-hyphenation": ["error", "always"],
    },
  },
  eslintConfigPrettier,
  oxlint.configs["flat/recommended"],
  {
    rules: {
      "vue/html-self-closing": [
        "error",
        {
          html: { void: "always", normal: "never", component: "always" },
          svg: "always",
          math: "always",
        },
      ],
    },
  },
);
