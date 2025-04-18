import asyncio
import datetime
import json
import time
from typing import Type
from warnings import filterwarnings

import shap
import requests
import matplotlib
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from llama_cpp import Llama
from lightgbm import LGBMClassifier
from imblearn.under_sampling import RandomUnderSampler
from skopt import BayesSearchCV
from skopt.space import Integer, Categorical, Iterable
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import GridSearchCV, train_test_split
from sklearn.metrics import classification_report

from src.analysis.core import IAnalysisRepository, IFileStorage
from src.analysis.schemas import Message
from src.config import settings

matplotlib.use("agg")
filterwarnings("ignore")


class AnalysisService:
    __wcss: dict = {}                  # Сумма внутрикластерных расстояний
    __percentage_error_diff = {}       # Процентные изменения WCSS
    __optimality_threshold: int = 20   # Порог разницы процентного прироста для определения оптимального количества кластеров
    __optimal_num_cluster: int = 3     # Оптимальное количество кластеров
    __efficiency_columns: list = ["Показы", "Взвешенные показы", "Клики", "CTR (%)", "wCTR (%)", "Расход (руб.)", "Ср. цена клика (руб.)", "Ср. ставка за клик (руб.)", "Отказы (%)", "Глубина (стр.)", "Прибыль (руб.)",]
    __cluster_img: np.ndarray
    __wcss_img: np.ndarray
    __llm: Llama = Llama(model_path="/home/egor/Develop/llm/llama.cpp/builds/mistral-7b-instruct-v0.1.Q4_K_M.gguf", verbose=False)

    # Dependency Inversion & Injection
    def __init__(
            self,
            repository: Type[IAnalysisRepository],
            filestorage: Type[IFileStorage]
    ):
        self.__repository = repository()
        self.__filestorage = filestorage()

    @staticmethod
    def __preprocessing_company_dataframe(company_df: pd.DataFrame) -> pd.DataFrame:
        company_df.replace({',': '.'}, regex=True, inplace=True)
        company_df.replace({'--': -1}, regex=False, inplace=True)
        ignored_cols = ["№ Группы"]
        for col in company_df.columns:
            try:
                if col in ignored_cols:
                    continue
                isinstance(float(company_df[col][0]), float)
                company_df[col] = company_df[col].astype("float64")
            except ValueError:
                continue
        return company_df

    async def __download_report_from_direct_api(self, report_id: int, token: str, report_name: str) -> pd.DataFrame | None:
        headers = {
            'Authorization': f'Bearer {token}',
            'Accept-Language': f'en',
            'processingMode': "offline",
            'returnMoneyInMicros': "true",
            'skipReportSummary': "true",
            'skipReportHeader': "true",
            'skipColumnHeader': "true",
        }
        payload = {
            "params": {
                "SelectionCriteria": {
                    "Filter": [{
                        "Field": "CampaignId",
                        "Operator": "EQUALS",
                        "Values": [f"{report_id}"]
                    }],
                    "DateFrom": "2023-09-22",
                    "DateTo": "2025-03-06",
                },
                "FieldNames": [
                    "Date",  # Дата
                    "AdGroupName",  # Группа
                    "AdGroupId",  # № Группы
                    "AdId",  # № Объявления
                    "AdNetworkType",  # Тип площадки
                    "TargetingLocationName",  # Регион таргетинга
                    "LocationOfPresenceName",  # Регион местонахождения
                    "Gender",  # Пол
                    "IncomeGrade",  # Уровень платежеспособности
                    "Age",  # Возраст
                    "MobilePlatform",  # Версия ОС устройства
                    "Impressions",  # Показы
                    "WeightedImpressions",  # Взвешенные показы
                    "Clicks",  # Клики
                    "Ctr",  # CTR (%)
                    "WeightedCtr",  # wCTR (%)
                    "Cost",  # Расход (руб.)
                    "AvgCpc",  # Ср. цена клика (руб.)
                    "AvgEffectiveBid",  # Ср. ставка за клик (руб.)
                    "BounceRate",  # Отказы (%)
                    "AvgPageviews",  # Глубина (стр.)
                    "GoalsRoi",  # Рентабельность
                    "Profit"  # Прибыль (руб.)
                ],
                "ReportName": f"{report_name}",
                "ReportType": "AD_PERFORMANCE_REPORT",
                "DateRangeType": "CUSTOM_DATE",
                "Format": "TSV",
                "IncludeVAT": "YES",
            }
        }
        response = requests.post(url=settings.REPORT_SERVICE_URL, headers=headers, json=payload)
        if response.status_code == 200:
            lines = response.text.rstrip().split("\n")
            data_dict = {
                "Дата": [],  # Date
                "Группа": [],  # AdGroupName
                "№ Группы": [],  # AdGroupId
                "№ Объявления": [],  # AdId
                "Тип площадки": [],  # AdNetworkType
                "Регион таргетинга": [],  # TargetingLocationName
                "Регион местонахождения": [],  # LocationOfPresenceName
                "Пол": [],  # Gender
                "Уровень платежеспособности": [],  # IncomeGrade
                "Возраст": [],  # Age
                "Версия ОС устройства": [],  # MobilePlatform
                "Показы": [],  # Impressions
                "Взвешенные показы": [],  # WeightedImpressions
                "Клики": [],  # Clicks
                "CTR (%)": [],  # Ctr
                "wCTR (%)": [],  # WeightedCtr
                "Расход (руб.)": [],  # Cost
                "Ср. цена клика (руб.)": [],  # AvgCpc
                "Ср. ставка за клик (руб.)": [],  # AvgEffectiveBid
                "Отказы (%)": [],  # BounceRate
                "Глубина (стр.)": [],  # AvgPageviews
                "Рентабельность": [],  # GoalsRoi
                "Прибыль (руб.)": []  # Profit
            }
            # Заполняем словарь данными из ответа
            for line in lines:
                values = line.split("\t")  # Разделяем по табуляции
                data_dict["Дата"].append(values[0])
                data_dict["Группа"].append(values[1])
                data_dict["№ Группы"].append(values[2])
                data_dict["№ Объявления"].append(values[3])
                data_dict["Тип площадки"].append(values[4])
                data_dict["Регион таргетинга"].append(values[5])
                data_dict["Регион местонахождения"].append(values[6])
                data_dict["Пол"].append(values[7])
                data_dict["Уровень платежеспособности"].append(values[8])
                data_dict["Возраст"].append(values[9])
                data_dict["Версия ОС устройства"].append(values[10])
                data_dict["Показы"].append(values[11])
                data_dict["Взвешенные показы"].append(values[12])
                data_dict["Клики"].append(values[13])
                data_dict["CTR (%)"].append(values[14])
                data_dict["wCTR (%)"].append(values[15])
                data_dict["Расход (руб.)"].append(values[16])
                data_dict["Ср. цена клика (руб.)"].append(values[17])
                data_dict["Ср. ставка за клик (руб.)"].append(values[18])
                data_dict["Отказы (%)"].append(values[19])
                data_dict["Глубина (стр.)"].append(values[20])
                data_dict["Рентабельность"].append(values[21])
                data_dict["Прибыль (руб.)"].append(values[22])
            return pd.DataFrame(data_dict)[["Возраст", "Пол", "Показы", "Взвешенные показы", "Клики", "CTR (%)", "wCTR (%)", "Расход (руб.)", "Ср. цена клика (руб.)", "Ср. ставка за клик (руб.)", "Отказы (%)", "Глубина (стр.)", "Прибыль (руб.)"]]
        elif response.status_code == 202 or response.status_code == 201:
            print("recursive step")
            await asyncio.sleep(10)
            return await self.__download_report_from_direct_api(
                report_id=report_id,
                token=token,
                report_name=report_name
            )
        else:
            print(response.text)
            return None

    def __cluster_advertising_company(self, company_df: pd.DataFrame) -> pd.DataFrame:
        print("start clustering")
        if "cluster_id" in self.__efficiency_columns:
            self.__efficiency_columns.remove("cluster_id")

        # Стандартизация данных
        scaler = StandardScaler()
        scaled_data = scaler.fit_transform(company_df[self.__efficiency_columns])

        # Применение PCA
        pca = PCA(n_components=2)
        pca_result = pca.fit_transform(scaled_data)

        # Создание DataFrame с PCA результатами
        pca_df = pd.DataFrame(pca_result, columns=['pca_1', 'pca_2'])

        self.__wcss = {}
        times = {}

        for i in range(1, 11):
            kmeans = KMeans(n_clusters=i, max_iter=300, init="k-means++", random_state=42)
            start_time = time.time()
            self.__wcss[i] = kmeans.fit(scaled_data).inertia_
            times[i] = time.time() - start_time

        # Визуализация метода локтя
        plt.figure(figsize=(10, 6))
        plt.title("Выбор оптимального количества кластеров методом локтя")
        ax1 = plt.gca()
        ax1.set_xlabel("Количество кластеров")
        ax1.set_ylabel("WCSS", color="blue")
        ax1.plot(self.__wcss.keys(), self.__wcss.values(), marker='o', color="blue")
        ax1.tick_params(axis='y', labelcolor="blue")

        ax2 = ax1.twinx()
        ax2.set_ylabel("Время обучения (сек)", color="red")
        ax2.plot(times.keys(), times.values(), marker='s', linestyle='dashed', color="red")
        ax2.tick_params(axis='y', labelcolor="red")

        plt.xticks(range(1, 11))
        plt.axvline(self.__optimal_num_cluster, color="indianred", linestyle="--")
        fig = plt.gcf()
        fig.canvas.draw()
        self.__wcss_img = np.array(fig.canvas.renderer.buffer_rgba())
        plt.close()

        # Определение оптимального числа кластеров
        for item in range(len(self.__percentage_error_diff) - 1):
            proc_diff = list(self.__percentage_error_diff.items())[item][1] - \
                        list(self.__percentage_error_diff.items())[item + 1][1]
            if proc_diff > self.__optimality_threshold:
                self.__optimal_num_cluster = int(list(self.__percentage_error_diff.items())[item][0][-1])
                break
            else:
                self.__optimal_num_cluster = 3

        # Кластеризация на PCA данных
        kmeans = KMeans(n_clusters=self.__optimal_num_cluster, max_iter=1000, init="k-means++", random_state=42)
        predict = kmeans.fit_predict(pca_result)

        # Добавление результатов в исходный DataFrame
        company_df = company_df.copy()
        company_df["cluster_id"] = predict
        company_df["pca_1"] = pca_df['pca_1']  # Добавляем первую компоненту PCA
        company_df["pca_2"] = pca_df['pca_2']  # Добавляем вторую компоненту PCA

        # Визуализация кластеров
        centroids = kmeans.cluster_centers_
        plt.figure(figsize=(10, 6))
        for i in np.unique(predict):
            plt.scatter(pca_df.iloc[predict == i, 0], pca_df.iloc[predict == i, 1], label=f'Кластер {i}')
        plt.scatter(centroids[:, 0], centroids[:, 1], s=200, c='black', marker='^', label='Центры кластеров')
        plt.legend(loc='lower right')
        plt.xlabel('Первая главная компонента')
        plt.ylabel('Вторая главная компонента')
        plt.title('Визуализация кластеров с центроидами')
        plt.tight_layout()
        fig = plt.gcf()
        fig.canvas.draw()
        self.__cluster_img = np.array(plt.gcf().canvas.renderer.buffer_rgba())
        plt.close()

        return company_df

    def __interpret_clusters(self, clustered_company_df: pd.DataFrame) -> pd.DataFrame:
        print("Light gradient boost start kill my intel...")
        sampler = RandomUnderSampler()
        X_resample, y_resample = sampler.fit_resample(
            X=pd.get_dummies(clustered_company_df[self.__efficiency_columns]),
            y=clustered_company_df["cluster_id"],
        )
        X_train, X_test, y_train, y_test = train_test_split(X_resample, y_resample)
        # Search model params
        optimizer = BayesSearchCV(
            estimator=LGBMClassifier(random_state=42, verbose=-1, n_jobs=-1),
            search_spaces={
                "n_estimators": Categorical([100, 200, 300, 400, 500]),
                "max_depth": Categorical([1, 2, 3, 4, 5]),
                "min_child_samples": Categorical([10, 20, 30, 40, 50]),
                "min_data_in_leaf": Categorical([10, 20, 30, 40, 50]),
            },
            cv=5,
            random_state=42,
        )
        optimizer.fit(X_train, y_train)
        # Create estimator for personal company data
        estimator = LGBMClassifier(**optimizer.best_params_)
        estimator.fit(X_train, y_train)
        # y_pred = estimator.predict(X_test)
        # print(classification_report(y_pred=y_pred, y_true=y_test))
        # Get shap impact value foreach cluster
        explainer = shap.TreeExplainer(estimator)
        shap_values = explainer.shap_values(X_test)
        shap_impact_df = pd.DataFrame(np.abs(shap_values).mean(axis=0), index=X_test.columns)
        return shap_impact_df

    # async def __get_llm_response(self, impact_df: pd.DataFrame):
    #     return self.__llm(
    #         prompt=settings.PATH_TO_LLM_PROMPT.read_text() + str(impact_df),
    #         max_tokens=settings.MAX_LLM_TOKENS,
    #     )["choices"][0]["text"] # noqa

    async def __define_bad_segments(self, clustered_company_df: pd.DataFrame, rejection_threshold: int = 100):
        rejection_result = []
        result = {}
        for j in range(self.__optimal_num_cluster):

            group = clustered_company_df.query(f"cluster_id=={j}").groupby(["Пол", "Возраст"])[
                ["CTR (%)", "Ср. цена клика (руб.)", "Отказы (%)", "Глубина (стр.)", "Расход (руб.)", 'Взвешенные показы',
                 'Клики', ]
            ].quantile(.5)
            for i, b in group[group["Отказы (%)"] >= rejection_threshold].index:
                if i != "не определен" or b != "не определен":
                    rejection_result.append(f"{i}: {b}")
            result[j] = rejection_result if rejection_result else "не выявлено"
        return result

    async def kill_cpu_and_gpu_by_lgbm(self, message: Message) -> None:
        """
        TODO docs
        """
        company_df = await self.__download_report_from_direct_api(
            report_id=message.company_id,
            token=message.yandex_id_token,
            report_name=message.report_name
        )
        if company_df is None:
            await self.__repository.update_company_report_info(
                report_id=message.company_id,
                is_ready=False,
                info="Error: report cannot be generated offline."
            )
            return
        company_df = self.__preprocessing_company_dataframe(company_df=company_df)
        clustered_company_df = self.__cluster_advertising_company(company_df=company_df)
        bad_segments = await self.__define_bad_segments(clustered_company_df=clustered_company_df, rejection_threshold=50)
        bad_segments = json.dumps(bad_segments, ensure_ascii=False)
        shap_impact_df = self.__interpret_clusters(clustered_company_df=clustered_company_df)
        impact_filename = f"{datetime.datetime.now(datetime.UTC).timestamp()}_shap_impact.csv"
        clustered_filename = f"{datetime.datetime.now(datetime.UTC).timestamp()}_clustered.csv"
        # llm_response = self.__get_llm_response(shap_impact_df)
        await self.__filestorage.upload_file(
            bucket=settings.S3_BUCKET,
            key=impact_filename,
            file=shap_impact_df.to_csv().encode()
        )
        await self.__filestorage.upload_file(
            bucket=settings.S3_BUCKET,
            key=clustered_filename,
            file=clustered_company_df.to_csv().encode()
        )
        await self.__repository.update_company_report_info(
            report_id=message.company_id,
            is_ready=True,
            info=f"Successful analysis AC.",
            bad_segments=bad_segments,
            path_to_clustered_df=clustered_filename,
            path_to_impact_df=impact_filename,
        )
