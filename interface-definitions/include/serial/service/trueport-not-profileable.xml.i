<!-- include start from serial/service/trueport-not-profileable.xml.i -->
<node name="trueport">
  <properties>
    <help>Trueport service settings</help>
  </properties>
  <children>
    <node name="server">
       <properties>
          <help>Trueport server settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/listen-port.xml.i>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
