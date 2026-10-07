---
layout: archive
title: "CV"
permalink: /cv/
author_profile: true
redirect_from:
  - /resume
---

{% include base_path %}

Education
======
* PhD in Computer Science, Dalhousie University, September 2025–present
  * Supervised by Ga Wu
  * Began graduate studies in September 2024; transferred to the PhD program in September 2025
* Honours Bachelor of Applied Science in Electrical Engineering, University of Waterloo, September 2013–June 2018

Work experience
======
* Software Engineer II, Microsoft, Vancouver, BC, July 2022–August 2024
  * Developed C++ components for Bing Ads selection and built and optimized data pipelines for machine-learning training
* Software Engineer, then Senior Data Engineer, Royal Bank of Canada (RBC), Toronto, ON, March 2019–June 2022
  * Developed Java Spring and Kafka applications for low-latency, fault-tolerant transaction messaging and implemented batch data processing in MemSQL

Research interests
======
* Reliable language-model systems
* Conformal prediction and retrieval-augmented generation
* Error attribution in multi-agent systems

Skills
======
* Programming: Python, Java, C++, C#, Angular
* Data analysis and data engineering

Publications
======
{% assign publications = site.publications | sort: "sort_order" | reverse %}
  <ul>{% for post in publications %}
    {% include archive-single-cv.html %}
  {% endfor %}</ul>
