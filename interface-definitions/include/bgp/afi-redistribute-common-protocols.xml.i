<!-- include start from bgp/afi-redistribute-common-protocols.xml.i -->
<node name="babel">
  <properties>
    <help>Redistribute Babel routes into BGP</help>
  </properties>
  <children>
    #include <include/bgp/afi-redistribute-metric-route-map.xml.i>
  </children>
</node>
<node name="connected">
  <properties>
    <help>Redistribute connected routes into BGP</help>
  </properties>
  <children>
    #include <include/bgp/afi-redistribute-metric-route-map.xml.i>
  </children>
</node>
<node name="kernel">
  <properties>
    <help>Redistribute kernel routes into BGP</help>
  </properties>
  <children>
    #include <include/bgp/afi-redistribute-metric-route-map.xml.i>
  </children>
</node>
<node name="static">
  <properties>
    <help>Redistribute static routes into BGP</help>
  </properties>
  <children>
    #include <include/bgp/afi-redistribute-metric-route-map.xml.i>
  </children>
</node>
<leafNode name="table">
  <properties>
    <help>Redistribute non-main Kernel Routing Table</help>
  </properties>
</leafNode>
<!-- include end -->
